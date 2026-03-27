"""Volume-packed local BlobTransport — append-only volume files for CAS.

Wraps the Rust VolumeEngine (nexus_fast.VolumeEngine) and implements the
BlobTransport protocol. Routes CAS blob keys (cas/...) to the volume engine
and delegates directory operations (dirs/...) to an internal LocalBlobTransport.

Volume engine benefits:
    - Packs thousands of blobs into append-only volume files
    - Reduces inode overhead from ~256-536 bytes/file to ~24 bytes/entry
    - Single pread() per content read (no directory traversal)
    - Batched fdatasync at seal time (not per-blob)
    - redb index for O(1) hash → (volume, offset, size) lookup

Crash recovery:
    - Active volumes are .tmp files — deleted on startup
    - Sealed volumes have TOC at end — can rebuild index from TOCs
    - Startup reconciliation handles all crash scenarios

Issue #3403: CAS volume packing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from nexus.backends.transports.local_transport import LocalBlobTransport
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

logger = logging.getLogger(__name__)

# Key prefix that routes to the volume engine (CAS content blobs)
_CAS_PREFIX = "cas/"


class VolumeLocalTransport:
    """Volume-packed BlobTransport for local CAS storage.

    Implements the full BlobTransport protocol (structural typing).
    CAS blob keys (cas/...) are routed to the Rust VolumeEngine.
    All other keys (dirs/..., uploads/...) are handled by an internal
    LocalBlobTransport for filesystem-native directory operations.

    Args:
        root_path: Root directory for all storage.
        fsync: Whether LocalBlobTransport uses fsync (for dirs/uploads).
        target_volume_size: Override volume size in bytes (0 = dynamic).
        compaction_rate_limit: I/O rate limit for compaction (bytes/sec).
        compaction_sparsity_threshold: Trigger compaction above this (0.0-1.0).
    """

    transport_name: str = "volume_local"

    def __init__(
        self,
        root_path: str | Path,
        *,
        fsync: bool = True,
        target_volume_size: int = 0,
        compaction_rate_limit: int = 52_428_800,
        compaction_sparsity_threshold: float = 0.4,
    ) -> None:
        self._root = Path(root_path).resolve()

        # Volume engine for CAS blobs (Rust)
        volumes_dir = self._root / "cas_volumes"
        try:
            from nexus_fast import VolumeEngine

            self._engine = VolumeEngine(
                str(volumes_dir),
                target_volume_size,
                compaction_rate_limit,
                compaction_sparsity_threshold,
            )
            self._volume_available = True
            logger.info("CAS volume engine initialized at %s", volumes_dir)
        except ImportError:
            self._engine = None
            self._volume_available = False
            logger.warning(
                "nexus_fast.VolumeEngine not available, "
                "falling back to file-per-blob LocalBlobTransport"
            )

        # Delegate transport for non-CAS keys (dirs, uploads, etc.)
        # Also serves as fallback if VolumeEngine is unavailable.
        self._delegate = LocalBlobTransport(root_path=root_path, fsync=fsync)

    def _is_cas_key(self, key: str) -> bool:
        """Check if a key should be routed to the volume engine."""
        return self._volume_available and key.startswith(_CAS_PREFIX) and not key.endswith(".meta")

    def _hash_from_key(self, key: str) -> str:
        """Extract content hash from a CAS key like 'cas/ab/cd/abcdef...'."""
        return key.split("/")[-1]

    # === BlobTransport Protocol Methods ===

    def put_blob(self, key: str, data: bytes, content_type: str = "") -> str | None:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                self._engine.put(hash_hex, data)
                return None
            except Exception as e:
                raise BackendError(
                    f"Volume put failed: {e}", backend="volume_local", path=key
                ) from e
        return self._delegate.put_blob(key, data, content_type)

    def get_blob(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                data = self._engine.get(hash_hex)
                if data is None:
                    raise NexusFileNotFoundError(key)
                return bytes(data), None
            except NexusFileNotFoundError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Volume get failed: {e}", backend="volume_local", path=key
                ) from e
        return self._delegate.get_blob(key, version_id)

    def delete_blob(self, key: str) -> None:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                existed = self._engine.delete(hash_hex)
                if not existed:
                    raise NexusFileNotFoundError(key)
                return
            except NexusFileNotFoundError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Volume delete failed: {e}", backend="volume_local", path=key
                ) from e
        self._delegate.delete_blob(key)

    def blob_exists(self, key: str) -> bool:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                return bool(self._engine.exists(hash_hex))
            except Exception:
                return False
        return self._delegate.blob_exists(key)

    def get_blob_size(self, key: str) -> int:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                size = self._engine.get_size(hash_hex)
                if size is None:
                    raise NexusFileNotFoundError(key)
                return int(size)
            except NexusFileNotFoundError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Volume get_size failed: {e}", backend="volume_local", path=key
                ) from e
        return self._delegate.get_blob_size(key)

    def list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        if prefix.startswith(_CAS_PREFIX) and self._volume_available:
            # Volume engine doesn't have directories — synthesize from index
            hashes_ts = self._engine.list_content_hashes()
            blob_keys = [f"cas/{h[:2]}/{h[2:4]}/{h}" for h, _ts in hashes_ts]
            if delimiter:
                # Filter to keys matching prefix at current level
                matching = [k for k in blob_keys if k.startswith(prefix)]
                return sorted(matching), []
            return sorted(blob_keys), []
        return self._delegate.list_blobs(prefix, delimiter)

    def copy_blob(self, src_key: str, dst_key: str) -> None:
        if self._is_cas_key(src_key) and self._is_cas_key(dst_key):
            # CAS copy = read from source volume, write to active volume
            data, _ = self.get_blob(src_key)
            self.put_blob(dst_key, data)
            return
        if self._is_cas_key(src_key):
            data, _ = self.get_blob(src_key)
            self._delegate.put_blob(dst_key, data)
            return
        if self._is_cas_key(dst_key):
            data, _ = self._delegate.get_blob(src_key)
            self.put_blob(dst_key, data)
            return
        self._delegate.copy_blob(src_key, dst_key)

    def create_directory_marker(self, key: str) -> None:
        # Always delegate to filesystem transport (volumes don't have directories)
        self._delegate.create_directory_marker(key)

    def stream_blob(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        if self._is_cas_key(key):
            # Read full blob from volume, yield in chunks
            data, _ = self.get_blob(key)
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]
            return
        yield from self._delegate.stream_blob(key, chunk_size, version_id)

    # === Extended Methods (used by CASAddressingEngine via hasattr) ===

    def put_blob_nosync(self, key: str, data: bytes) -> None:
        """Write without fsync — for reconstructable metadata.

        Volume engine batches fsync at seal time, so this is the same as put_blob.
        """
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                self._engine.put(hash_hex, data)
            except Exception as e:
                raise BackendError(
                    f"Volume put_nosync failed: {e}", backend="volume_local", path=key
                ) from e
            return
        self._delegate.put_blob_nosync(key, data)

    def put_blob_from_path(self, key: str, src_path: str | Path) -> str | None:
        """Move a file into the volume (read file, append to volume, delete source)."""
        if self._is_cas_key(key):
            src = Path(src_path)
            try:
                data = src.read_bytes()
                hash_hex = self._hash_from_key(key)
                self._engine.put(hash_hex, data)
                src.unlink(missing_ok=True)
                return None
            except Exception as e:
                raise BackendError(
                    f"Volume put_from_path failed: {e}", backend="volume_local", path=key
                ) from e
        return self._delegate.put_blob_from_path(key, src_path)

    def get_blob_mtime(self, key: str) -> float:
        """Blob write timestamp. For GC age threshold."""
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            try:
                ts = self._engine.get_timestamp(hash_hex)
                if ts is None:
                    raise NexusFileNotFoundError(key)
                return float(ts)
            except NexusFileNotFoundError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Volume get_mtime failed: {e}", backend="volume_local", path=key
                ) from e
        return self._delegate.get_blob_mtime(key)

    # === New Methods (transport protocol extensions) ===

    def list_content_hashes(self) -> list[tuple[str, float]]:
        """List all content hashes with write timestamps.

        Returns list of (hash_hex, timestamp_secs) tuples.
        Used by GC for reachability scan and by Bloom filter for seeding.
        """
        if self._volume_available:
            try:
                return list(self._engine.list_content_hashes())
            except Exception as e:
                logger.warning("Volume list_content_hashes failed: %s", e)
                return []
        # Fallback: scan filesystem
        return self._delegate.list_content_hashes()

    def batch_get_blobs(self, keys: list[str]) -> dict[str, bytes | None]:
        """Batch read multiple blobs efficiently.

        Groups CAS reads into a single batch_get call to the volume engine
        for sequential I/O within volumes. Non-CAS keys are read individually.
        """
        result: dict[str, bytes | None] = {}
        cas_keys: dict[str, str] = {}  # key → hash_hex
        other_keys: list[str] = []

        for key in keys:
            if self._is_cas_key(key):
                cas_keys[key] = self._hash_from_key(key)
            else:
                other_keys.append(key)

        # Batch read CAS blobs via volume engine
        if cas_keys and self._volume_available:
            try:
                hash_to_key: dict[str, str] = {h: k for k, h in cas_keys.items()}
                batch_result = self._engine.batch_get(list(cas_keys.values()))
                for hash_hex, data in batch_result.items():
                    matched_key = hash_to_key.get(hash_hex)
                    if matched_key is not None:
                        key = matched_key
                        result[key] = bytes(data)
                # Fill missing with None
                for key in cas_keys:
                    if key not in result:
                        result[key] = None
            except Exception:
                # Fallback to individual reads
                for key in cas_keys:
                    try:
                        data, _ = self.get_blob(key)
                        result[key] = data
                    except Exception:
                        result[key] = None

        # Read non-CAS keys individually
        for key in other_keys:
            try:
                data, _ = self._delegate.get_blob(key)
                result[key] = data
            except Exception:
                result[key] = None

        return result

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

    def migrate_from_files(
        self,
        *,
        batch_size: int = 1000,
        delete_originals: bool = True,
        rate_limit_bytes: int = 0,
    ) -> tuple[int, int, int]:
        """Migrate existing one-file-per-hash CAS blobs into volumes.

        Scans the cas/ directory for files matching the cas/{h[:2]}/{h[2:4]}/{h}
        layout, packs them into volumes, and optionally deletes the originals.

        Args:
            batch_size: Files to migrate per batch before sealing (default 1000).
            delete_originals: Delete original files after migration (default True).
            rate_limit_bytes: Max bytes per call (0 = unlimited).

        Returns:
            (files_migrated, files_skipped, bytes_migrated)
        """
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

    # === Internal Helpers ===

    def move_blob(self, src_key: str, dst_key: str) -> None:
        """Atomic move — delegate to appropriate transport."""
        if self._is_cas_key(src_key) or self._is_cas_key(dst_key):
            # Cross-transport move = copy + delete
            data, _ = self.get_blob(src_key)
            self.put_blob(dst_key, data)
            self.delete_blob(src_key)
            return
        self._delegate.move_blob(src_key, dst_key)
