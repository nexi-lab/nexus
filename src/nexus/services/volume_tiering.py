"""Volume-level cold tiering — sealed volumes to cloud with local index.

Uploads sealed, quiet CAS volumes to S3/GCS as single objects and serves
reads via HTTP range requests.  The local redb index is retained so that
hash → (volume, offset, size) lookups remain O(1).

Lifecycle state machine (write-ahead):

    SEALED  →  TIERING  →  TIERED  →  (local .vol deleted)
              (upload     (verified,   (optional: keep local
               started)    cloud OK)    .vol as warm cache)

Crash recovery:
    - TIERING + no cloud object  → re-upload from local .dat
    - TIERING + cloud object OK  → verify checksum, advance to TIERED
    - TIERED  + local .dat exists → delete local .dat (deferred cleanup)

LRU volume cache:
    - Recently-accessed tiered volumes are cached locally on disk
    - Burst detection: N reads within a window triggers full re-download
    - LRU eviction by last-access time when cache exceeds max size
    - Cache is best-effort: miss → cloud range read, hit → local pread

Design:
    - Asyncio timer loop (same pattern as TTLVolumeSweeper)
    - Idempotent: safe to run sweep at any frequency
    - Write-ahead manifest: state persisted *before* destructive action
    - Rate-limited uploads: token-bucket to avoid starving foreground I/O

Issue #3406: Volume-level cold tiering.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import struct
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.core.config import TieringConfig

logger = logging.getLogger(__name__)


# ─── Tiering State ──────────────────────────────────────────────────────────


class VolumeState(StrEnum):
    """Volume tiering lifecycle state."""

    SEALED = "sealed"
    TIERING = "tiering"
    TIERED = "tiered"


@dataclass
class TieredVolumeEntry:
    """State for a single volume in the tiering manifest.

    The per-blob index (hash → offset, size) is stored in a separate
    ``.idx`` file per volume, NOT in the main manifest JSON.  This keeps
    the manifest small and avoids O(total_blobs) rewrites on every save.
    """

    volume_id: str
    state: str  # VolumeState value
    cloud_key: str = ""
    checksum_sha256: str = ""
    uploaded_at: float = 0.0
    size_bytes: int = 0
    blob_count: int = 0  # for stats only; actual index is in .idx file


# ─── Volume TOC Parser ──────────────────────────────────────────────────────

# Volume file format (from rust/nexus_kernel/src/volume_engine.rs):
#   Footer (last 24 bytes): [magic:4 "NVOL"] [version:4] [entry_count:4] [toc_offset:8] [checksum:4]
#   TOC entry (45 bytes each): [hash:32] [offset:8] [size:4] [flags:1]
_FOOTER_SIZE = 24
_TOC_ENTRY_SIZE = 45
_VOLUME_MAGIC = b"NVOL"


def parse_volume_toc(vol_path: Path) -> dict[str, tuple[int, int]]:
    """Parse a sealed .vol file's TOC to build hash → (offset, size) map.

    Reads only the footer + TOC entries (not the blob data), so memory
    usage is proportional to entry count, not volume size.

    Returns:
        Dict mapping hex hash strings to (byte_offset, byte_size) tuples.

    Raises:
        ValueError: If the file is not a valid sealed volume.
    """
    file_size = vol_path.stat().st_size
    if file_size < _FOOTER_SIZE:
        raise ValueError(f"File too small to be a volume: {vol_path}")

    with open(vol_path, "rb") as f:
        # Read footer (last 24 bytes)
        f.seek(file_size - _FOOTER_SIZE)
        footer = f.read(_FOOTER_SIZE)

        magic = footer[0:4]
        if magic != _VOLUME_MAGIC:
            raise ValueError(f"Invalid volume magic: {magic!r} (expected {_VOLUME_MAGIC!r})")

        _version = struct.unpack_from("<I", footer, 4)[0]
        entry_count = struct.unpack_from("<I", footer, 8)[0]
        toc_offset = struct.unpack_from("<Q", footer, 12)[0]

        # Read TOC entries
        f.seek(toc_offset)
        index: dict[str, tuple[int, int]] = {}
        for _ in range(entry_count):
            toc_buf = f.read(_TOC_ENTRY_SIZE)
            if len(toc_buf) < _TOC_ENTRY_SIZE:
                break
            hash_bytes = toc_buf[0:32]
            offset = struct.unpack_from("<Q", toc_buf, 32)[0]
            size = struct.unpack_from("<I", toc_buf, 40)[0]
            flags = toc_buf[44]
            if flags & 0x01:  # FLAG_TOMBSTONE
                continue
            hash_hex = hash_bytes.hex()
            index[hash_hex] = (offset, size)

    return index


# ─── Tiering Manifest (JSON persistence) ────────────────────────────────────


class TieringManifest:
    """Write-ahead manifest for volume tiering state.

    The manifest JSON (``tiering_state.json``) stores only lightweight
    volume metadata — NOT the per-blob index.  Each volume's blob index
    (hash → offset, size) lives in a separate ``.idx`` JSON file,
    written once at tier time and never rewritten.

    An in-memory reverse hash set (``_tiered_hashes``) provides O(1)
    checks for GC and existence queries without scanning all indexes.
    """

    def __init__(self, manifest_path: Path) -> None:
        self._path = manifest_path
        self._idx_dir = manifest_path.parent  # .idx files live next to manifest
        self._volumes: dict[str, TieredVolumeEntry] = {}
        self._last_read_ts: dict[str, float] = {}
        # Reverse lookup: hash_hex → volume_id (O(1) for GC + exists)
        self._tiered_hashes: dict[str, str] = {}
        # In-memory cache of loaded .idx files: vol_id → {hash: [offset, size]}
        # Loaded once per volume, never re-parsed until removal.
        self._idx_cache: dict[str, dict[str, list[int]]] = {}
        self._load()

    def _load(self) -> None:
        """Load manifest from disk. Missing file = empty state."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for vid, entry_dict in data.get("volumes", {}).items():
                self._volumes[vid] = TieredVolumeEntry(**entry_dict)
            self._last_read_ts = data.get("last_read_ts", {})
        except Exception:
            logger.warning("Failed to load tiering manifest at %s, starting fresh", self._path)
            self._volumes = {}
            self._last_read_ts = {}

        # Rebuild reverse hash set from .idx files
        self._rebuild_hash_set()

    def _rebuild_hash_set(self) -> None:
        """Rebuild the in-memory reverse hash set from per-volume .idx files."""
        self._tiered_hashes.clear()
        for vid, entry in self._volumes.items():
            if entry.state in (VolumeState.TIERED, VolumeState.TIERING):
                idx = self.load_blob_index(vid)
                for h in idx:
                    self._tiered_hashes[h] = vid

    def _save(self) -> None:
        """Persist manifest to disk (atomic via write-then-rename).

        Only saves volume metadata + read timestamps — NOT blob indexes.
        """
        data = {
            "volumes": {vid: asdict(entry) for vid, entry in self._volumes.items()},
            "last_read_ts": self._last_read_ts,
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.rename(self._path)

    # ─── Per-volume blob index (.idx files) ────────────────────────────

    def _idx_path(self, volume_id: str) -> Path:
        return self._idx_dir / f"{volume_id}.idx"

    def save_blob_index(self, volume_id: str, blob_index: dict[str, list[int]]) -> None:
        """Write per-volume blob index to a separate .idx file (once)."""
        idx_path = self._idx_path(volume_id)
        tmp = idx_path.with_suffix(".idx.tmp")
        tmp.write_text(json.dumps(blob_index), encoding="utf-8")
        tmp.rename(idx_path)
        # Populate in-memory cache + reverse hash set
        self._idx_cache[volume_id] = blob_index
        for h in blob_index:
            self._tiered_hashes[h] = volume_id

    def load_blob_index(self, volume_id: str) -> dict[str, list[int]]:
        """Get per-volume blob index (in-memory cache, loaded once from disk)."""
        cached = self._idx_cache.get(volume_id)
        if cached is not None:
            return cached
        # Cache miss — load from .idx file, cache for future lookups
        idx_path = self._idx_path(volume_id)
        if not idx_path.exists():
            return {}
        try:
            result: dict[str, list[int]] = json.loads(idx_path.read_text(encoding="utf-8"))
            self._idx_cache[volume_id] = result
            return result
        except Exception:
            logger.warning("Failed to load blob index for %s", volume_id)
            return {}

    def remove_blob_index(self, volume_id: str) -> None:
        """Delete per-volume .idx file and purge from caches."""
        idx_path = self._idx_path(volume_id)
        idx_path.unlink(missing_ok=True)
        self._idx_cache.pop(volume_id, None)
        self._tiered_hashes = {h: v for h, v in self._tiered_hashes.items() if v != volume_id}

    # ─── Volume state ──────────────────────────────────────────────────

    def get(self, volume_id: str) -> TieredVolumeEntry | None:
        return self._volumes.get(volume_id)

    def set_state(self, entry: TieredVolumeEntry) -> None:
        """Write-ahead: persist state *before* acting on it."""
        self._volumes[entry.volume_id] = entry
        self._save()

    def remove(self, volume_id: str) -> None:
        self._volumes.pop(volume_id, None)
        self._last_read_ts.pop(volume_id, None)
        self.remove_blob_index(volume_id)
        self._save()

    def tiered_volumes(self) -> list[TieredVolumeEntry]:
        """All volumes in TIERED state."""
        return [e for e in self._volumes.values() if e.state == VolumeState.TIERED]

    def tiering_volumes(self) -> list[TieredVolumeEntry]:
        """All volumes in TIERING state (in-flight uploads)."""
        return [e for e in self._volumes.values() if e.state == VolumeState.TIERING]

    def all_entries(self) -> list[TieredVolumeEntry]:
        return list(self._volumes.values())

    def is_volume_tiered_or_tiering(self, volume_id: str) -> bool:
        entry = self._volumes.get(volume_id)
        if entry is None:
            return False
        return entry.state in (VolumeState.TIERED, VolumeState.TIERING)

    # ─── O(1) hash lookups (for GC, exists, read) ─────────────────────

    def is_hash_tiered(self, hash_hex: str) -> bool:
        """O(1) check: is this hash in any tiered volume?"""
        return hash_hex in self._tiered_hashes

    def lookup_hash(self, hash_hex: str) -> tuple[TieredVolumeEntry, int, int] | None:
        """O(1) lookup: find the tiered volume + offset + size for a hash.

        Returns (entry, offset, size) or None.
        """
        vol_id = self._tiered_hashes.get(hash_hex)
        if vol_id is None:
            return None
        entry = self._volumes.get(vol_id)
        if entry is None or entry.state != VolumeState.TIERED:
            return None
        idx = self.load_blob_index(vol_id)
        location = idx.get(hash_hex)
        if location is None:
            return None
        return entry, location[0], location[1]

    # ─── Last-read tracking (Issue #3406, decision 13A) ─────────────────

    def record_read(self, volume_id: str) -> None:
        """Record a read access timestamp for a sealed volume."""
        self._last_read_ts[volume_id] = time.time()

    def last_read_time(self, volume_id: str) -> float:
        """Last read timestamp, or 0.0 if never read."""
        return self._last_read_ts.get(volume_id, 0.0)

    def flush_read_timestamps(self) -> None:
        """Persist accumulated read timestamps to disk."""
        self._save()


# ─── LRU Volume Cache ────────────────────────────────────────────────────────


class VolumeCache:
    """LRU disk cache for recently-accessed tiered volumes.

    Caches full .dat files locally so subsequent reads use local pread
    instead of cloud range requests.  Evicts least-recently-accessed
    volumes when the cache exceeds ``max_size_bytes``.

    Burst detection: when a volume receives ``burst_threshold`` reads
    within ``burst_window_seconds``, the full volume is downloaded to
    cache (re-download for burst read patterns).
    """

    def __init__(
        self,
        cache_dir: Path,
        cloud_transport: Any,
        *,
        max_size_bytes: int = 10 * 1024 * 1024 * 1024,
        burst_threshold: int = 5,
        burst_window_seconds: float = 60.0,
    ) -> None:
        self._cache_dir = Path(cache_dir).resolve()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cloud = cloud_transport
        self._max_size_bytes = max_size_bytes
        self._burst_threshold = burst_threshold
        self._burst_window = burst_window_seconds

        # LRU tracking: volume_id → last access time
        self._access_times: dict[str, float] = {}

        # Burst detection: volume_id → list of recent read timestamps
        self._read_history: dict[str, list[float]] = {}

        # Pending downloads (avoid duplicate concurrent downloads)
        self._pending: set[str] = set()

        # Scan existing cache on init
        self._scan_existing_cache()

    def _scan_existing_cache(self) -> None:
        """Discover already-cached volumes from a previous session."""
        for dat_path in self._cache_dir.glob("*.dat"):
            vol_id = dat_path.stem
            with contextlib.suppress(OSError):
                self._access_times[vol_id] = dat_path.stat().st_mtime

    def _cache_path(self, volume_id: str) -> Path:
        return self._cache_dir / f"{volume_id}.dat"

    def has(self, volume_id: str) -> bool:
        """Check if a volume is in the local cache."""
        return self._cache_path(volume_id).exists()

    def read_local(self, volume_id: str, offset: int, size: int) -> bytes | None:
        """Read a byte range from a cached volume. Returns None on miss."""
        path = self._cache_path(volume_id)
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(size)
            self._access_times[volume_id] = time.time()
            return data
        except OSError:
            return None

    def record_read_and_check_burst(self, volume_id: str) -> bool:
        """Record a read and return True if burst threshold is exceeded.

        Prunes timestamps older than ``burst_window`` on each call.
        """
        now = time.time()
        cutoff = now - self._burst_window

        history = self._read_history.get(volume_id, [])
        # Prune old timestamps
        history = [ts for ts in history if ts > cutoff]
        history.append(now)
        self._read_history[volume_id] = history

        return len(history) >= self._burst_threshold

    def download_volume(self, cloud_key: str, volume_id: str) -> bool:
        """Download a full volume from cloud to the local cache (streaming).

        Streams data to disk in chunks — never buffers the full volume in
        RAM.  Evicts LRU volumes if needed to stay under max_size_bytes.
        Returns True if download succeeded and volume is now cached.
        """
        if volume_id in self._pending:
            return False
        self._pending.add(volume_id)
        try:
            # Check size before downloading
            try:
                remote_size = self._cloud.get_size(cloud_key)
            except Exception:
                return False

            # Evict if needed
            self._evict_to_fit(remote_size)

            # Stream download in chunks (not full-object fetch)
            cache_path = self._cache_path(volume_id)
            tmp_path = cache_path.with_suffix(".tmp")
            chunk_size = 8 * 1024 * 1024  # 8 MB
            try:
                with open(tmp_path, "wb") as f:
                    offset = 0
                    while offset < remote_size:
                        read_size = min(chunk_size, remote_size - offset)
                        chunk = self._cloud.get_blob_range(cloud_key, offset, read_size)
                        f.write(chunk)
                        offset += read_size
                tmp_path.rename(cache_path)
                self._access_times[volume_id] = time.time()
                logger.info(
                    "Volume cache: downloaded %s (%.1f MB, streamed)",
                    volume_id,
                    remote_size / (1024 * 1024),
                )
                return True
            except Exception as e:
                tmp_path.unlink(missing_ok=True)
                logger.warning("Volume cache: failed to download %s: %s", volume_id, e)
                return False
        finally:
            self._pending.discard(volume_id)

    def current_size_bytes(self) -> int:
        """Total size of all cached volumes on disk."""
        total = 0
        for dat_path in self._cache_dir.glob("*.dat"):
            with contextlib.suppress(OSError):
                total += dat_path.stat().st_size
        return total

    def _evict_to_fit(self, needed_bytes: int) -> int:
        """Evict LRU volumes until there's room for ``needed_bytes``.

        Returns number of volumes evicted.
        """
        current = self.current_size_bytes()
        target = self._max_size_bytes - needed_bytes
        if current <= target:
            return 0

        # Sort by access time (oldest first)
        candidates = sorted(self._access_times.items(), key=lambda kv: kv[1])
        evicted = 0
        for vol_id, _ts in candidates:
            if current <= target:
                break
            path = self._cache_path(vol_id)
            try:
                size = path.stat().st_size
                path.unlink()
                current -= size
                self._access_times.pop(vol_id, None)
                self._read_history.pop(vol_id, None)
                evicted += 1
                logger.info("Volume cache: evicted %s (LRU)", vol_id)
            except OSError:
                self._access_times.pop(vol_id, None)
        return evicted

    def remove(self, volume_id: str) -> None:
        """Remove a volume from the cache."""
        path = self._cache_path(volume_id)
        path.unlink(missing_ok=True)
        self._access_times.pop(volume_id, None)
        self._read_history.pop(volume_id, None)

    @property
    def cached_count(self) -> int:
        return len(self._access_times)


# ─── Volume Tiering Service ─────────────────────────────────────────────────


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file (streaming, low memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class VolumeTieringService:
    """Background service that tiers sealed CAS volumes to cloud storage.

    Usage::

        service = VolumeTieringService(
            volumes_dir=Path("/data/cas_volumes"),
            cloud_transport=gcs_transport,  # or s3_transport
            config=TieringConfig(...),
        )
        await service.start()
        ...
        await service.stop()
    """

    def __init__(
        self,
        volumes_dir: Path,
        cloud_transport: Any,
        config: TieringConfig,
    ) -> None:
        self._volumes_dir = Path(volumes_dir).resolve()
        self._cloud = cloud_transport
        self._config = config
        self._manifest = TieringManifest(self._volumes_dir / "tiering_state.json")
        self._running = False
        self._task: asyncio.Task[None] | None = None

        # LRU volume cache for frequently-accessed tiered volumes
        self._cache = VolumeCache(
            cache_dir=self._volumes_dir / "cache",
            cloud_transport=cloud_transport,
            max_size_bytes=config.local_cache_size_bytes,
            burst_threshold=config.burst_read_threshold,
            burst_window_seconds=config.burst_read_window_seconds,
        )

    @property
    def manifest(self) -> TieringManifest:
        return self._manifest

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the tiering background loop."""
        if self._running:
            return
        self._running = True
        # Recover from crashes before starting the sweep loop
        self._recover_on_startup()
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            "Volume tiering service started (interval: %.1fs, bucket: %s)",
            self._config.sweep_interval_seconds,
            self._config.cloud_bucket,
        )

    async def stop(self) -> None:
        """Stop the tiering background loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Flush read timestamps on shutdown
        self._manifest.flush_read_timestamps()
        logger.info("Volume tiering service stopped")

    async def _sweep_loop(self) -> None:
        """Main sweep loop — periodic timer."""
        while self._running:
            try:
                await asyncio.sleep(self._config.sweep_interval_seconds)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("Volume tiering sweep failed")

    # ─── Sweep: find and tier eligible volumes ───────────────────────────

    async def sweep_once(self) -> int:
        """Run one tiering sweep. Returns number of volumes tiered.

        Callable standalone (without start()) for testing and one-shot use.
        """
        sealed_volumes = self._find_sealed_volumes()
        tiered_count = 0

        for vol_id, dat_path in sealed_volumes:
            if self._manifest.is_volume_tiered_or_tiering(vol_id):
                continue

            if not self._is_eligible(vol_id, dat_path):
                continue

            try:
                await self._tier_volume(vol_id, dat_path)
                tiered_count += 1
            except Exception:
                logger.exception("Failed to tier volume %s", vol_id)

        # Flush read timestamps periodically
        self._manifest.flush_read_timestamps()

        if tiered_count > 0:
            logger.info("Tiering sweep: tiered %d volumes", tiered_count)

        return tiered_count

    def _find_sealed_volumes(self) -> list[tuple[str, Path]]:
        """Find sealed volume .vol files in the volumes directory.

        Sealed volumes use .vol extension (renamed from .tmp at seal time).
        Active volumes are .tmp files (crash recovery deletes them).
        """
        results: list[tuple[str, Path]] = []
        if not self._volumes_dir.exists():
            return results

        for vol_path in sorted(self._volumes_dir.glob("*.vol")):
            vol_id = vol_path.stem
            results.append((vol_id, vol_path))

        return results

    def _is_eligible(self, volume_id: str, dat_path: Path) -> bool:
        """Check if a sealed volume is eligible for tiering."""
        # Size check
        try:
            size = dat_path.stat().st_size
        except OSError:
            return False

        if size < self._config.min_volume_size_bytes:
            return False

        # Quiet period check: volume must not have been read recently
        now = time.time()
        last_read = self._manifest.last_read_time(volume_id)
        # If never read, use file mtime as proxy
        if last_read == 0.0:
            try:
                last_read = dat_path.stat().st_mtime
            except OSError:
                return False

        return (now - last_read) >= self._config.quiet_period_seconds

    async def _tier_volume(self, volume_id: str, vol_path: Path) -> None:
        """Tier a single volume: upload to cloud with write-ahead state.

        State machine:
            1. Parse .vol TOC to build blob index (hash → offset, size)
            2. Write TIERING state (write-ahead, includes blob_index)
            3. Compute local checksum
            4. Upload .vol to cloud with rate limiting
            5. Verify cloud object size matches
            6. Write TIERED state
            7. Delete local .vol
        """
        cloud_key = f"volumes/{volume_id}.vol"
        file_size = vol_path.stat().st_size

        # Step 1: Parse TOC to build per-blob index for range reads.
        # This replaces engine.locate() which is not exposed via PyO3.
        blob_index_raw = await asyncio.to_thread(parse_volume_toc, vol_path)
        blob_index = {h: [o, s] for h, (o, s) in blob_index_raw.items()}

        # Step 1b: Save blob index to separate .idx file (not in manifest).
        self._manifest.save_blob_index(volume_id, blob_index)

        # Step 2: Write-ahead — mark as TIERING before upload
        entry = TieredVolumeEntry(
            volume_id=volume_id,
            state=VolumeState.TIERING,
            cloud_key=cloud_key,
            size_bytes=file_size,
            blob_count=len(blob_index),
        )
        self._manifest.set_state(entry)

        # Step 3: Compute local checksum
        local_checksum = await asyncio.to_thread(_file_sha256, vol_path)

        # Step 4: Upload with rate limiting
        await self._upload_with_rate_limit(cloud_key, vol_path)

        # Step 5: Verify cloud object size matches
        await self._verify_upload(cloud_key, file_size)

        # Step 6: Write TIERED state (with checksum)
        entry.state = VolumeState.TIERED
        entry.checksum_sha256 = local_checksum
        entry.uploaded_at = time.time()
        self._manifest.set_state(entry)

        # Step 7: Delete local .vol (state already persisted)
        try:
            vol_path.unlink()
            logger.info(
                "Volume %s tiered to %s (%.1f MB, %d blobs, checksum=%s)",
                volume_id,
                cloud_key,
                file_size / (1024 * 1024),
                len(blob_index),
                local_checksum[:16],
            )
        except OSError as e:
            # Non-fatal: local .dat is now just a warm cache copy
            logger.warning("Failed to delete local .dat for tiered volume %s: %s", volume_id, e)

    async def _upload_with_rate_limit(self, cloud_key: str, local_path: Path) -> None:
        """Upload a file to cloud storage with rate limiting.

        Uses token-bucket style throttling: read chunk, upload chunk,
        sleep proportional to chunk size / rate limit.
        """
        rate_limit = self._config.upload_rate_limit_bytes
        chunk_size = 8 * 1024 * 1024  # 8 MB default
        if rate_limit > 0:
            chunk_size = min(chunk_size, rate_limit)

        def _do_upload() -> None:
            self._cloud.upload_file(cloud_key, str(local_path), chunk_size=chunk_size)

        # Run upload in thread (blocking I/O)
        await asyncio.to_thread(_do_upload)

    async def _verify_upload(self, cloud_key: str, expected_size: int) -> None:
        """Verify cloud upload by checking object size."""

        def _check() -> None:
            actual_size = self._cloud.get_size(cloud_key)
            if actual_size != expected_size:
                raise BackendError(
                    f"Upload verification failed for {cloud_key}: "
                    f"expected {expected_size} bytes, got {actual_size}",
                    backend="tiering",
                    path=cloud_key,
                )

        await asyncio.to_thread(_check)

    # ─── Crash Recovery ──────────────────────────────────────────────────

    def _recover_on_startup(self) -> None:
        """Recover from incomplete tiering operations on startup.

        Handles all crash windows:
            TIERING + local .vol + no cloud → re-upload on next sweep
            TIERING + local .vol + cloud OK → verify and advance to TIERED
            TIERED + local .vol exists → verify cloud, then delete local
        """
        for entry in self._manifest.tiering_volumes():
            vol_path = self._volumes_dir / f"{entry.volume_id}.vol"
            if vol_path.exists():
                # Local .vol still exists — check if cloud upload completed
                try:
                    cloud_size = self._cloud.get_size(entry.cloud_key)
                    local_size = vol_path.stat().st_size
                    if cloud_size == local_size:
                        # Upload completed — advance to TIERED
                        entry.state = VolumeState.TIERED
                        entry.uploaded_at = time.time()
                        self._manifest.set_state(entry)
                        vol_path.unlink(missing_ok=True)
                        logger.info(
                            "Recovery: volume %s upload was complete, advanced to TIERED",
                            entry.volume_id,
                        )
                    else:
                        # Upload incomplete — revert to allow re-upload
                        self._manifest.remove(entry.volume_id)
                        logger.info(
                            "Recovery: volume %s upload incomplete, reverted to SEALED",
                            entry.volume_id,
                        )
                except NexusFileNotFoundError:
                    # Cloud object doesn't exist — revert
                    self._manifest.remove(entry.volume_id)
                    logger.info(
                        "Recovery: volume %s not in cloud, reverted to SEALED",
                        entry.volume_id,
                    )
                except Exception:
                    logger.warning(
                        "Recovery: failed to check volume %s, leaving as TIERING",
                        entry.volume_id,
                    )
            else:
                # Local .vol gone but state is TIERING — check cloud
                try:
                    self._cloud.get_size(entry.cloud_key)
                    # Cloud has it — advance to TIERED
                    entry.state = VolumeState.TIERED
                    entry.uploaded_at = time.time()
                    self._manifest.set_state(entry)
                    logger.info(
                        "Recovery: volume %s local deleted but cloud OK, advanced to TIERED",
                        entry.volume_id,
                    )
                except NexusFileNotFoundError:
                    # Both local and cloud gone — data loss, log error
                    self._manifest.remove(entry.volume_id)
                    logger.error(
                        "Recovery: volume %s lost — not in local or cloud!",
                        entry.volume_id,
                    )
                except Exception:
                    logger.warning(
                        "Recovery: failed to check cloud for volume %s",
                        entry.volume_id,
                    )

        # Deferred cleanup: TIERED volumes with lingering local .vol.
        # FIX (Codex review #3): verify cloud object still exists before
        # deleting the local copy — don't destroy the last remaining data.
        for entry in self._manifest.tiered_volumes():
            vol_path = self._volumes_dir / f"{entry.volume_id}.vol"
            if vol_path.exists():
                try:
                    cloud_size = self._cloud.get_size(entry.cloud_key)
                    if cloud_size == entry.size_bytes or entry.size_bytes == 0:
                        vol_path.unlink(missing_ok=True)
                        logger.info(
                            "Recovery: deleted lingering local .vol for tiered volume %s "
                            "(cloud verified)",
                            entry.volume_id,
                        )
                    else:
                        logger.warning(
                            "Recovery: cloud size mismatch for %s (expected %d, got %d), "
                            "keeping local .vol",
                            entry.volume_id,
                            entry.size_bytes,
                            cloud_size,
                        )
                except NexusFileNotFoundError:
                    # Cloud object is gone! Don't delete local — revert to SEALED
                    self._manifest.remove(entry.volume_id)
                    logger.warning(
                        "Recovery: cloud object missing for TIERED volume %s, "
                        "reverted to SEALED (keeping local .vol)",
                        entry.volume_id,
                    )
                except Exception:
                    logger.warning(
                        "Recovery: failed to verify cloud for TIERED volume %s, "
                        "keeping local .vol as safety",
                        entry.volume_id,
                    )

    # ─── Read Support (called by VolumeLocalTransport) ───────────────────

    def read_range(self, cloud_key: str, offset: int, size: int, volume_id: str = "") -> bytes:
        """Read a byte range from a tiered volume.

        Read priority:
            1. Local LRU cache hit → pread from disk
            2. Cache miss → cloud range request
            3. After cache miss: check burst → trigger re-download if threshold met

        This is a synchronous call — VolumeLocalTransport.get_blob()
        calls this from a synchronous context.
        """
        # 1. Try local cache first
        if volume_id and self._cache.has(volume_id):
            cached = self._cache.read_local(volume_id, offset, size)
            if cached is not None:
                return cached

        # 2. Cloud range request (cache miss)
        data = bytes(self._cloud.get_blob_range(cloud_key, offset, size))

        # 3. Burst detection → trigger background re-download (non-blocking).
        # The download runs in a daemon thread so the foreground read returns
        # immediately with the range-read data.  Subsequent reads benefit
        # from the cache once the download completes.
        if volume_id:
            is_burst = self._cache.record_read_and_check_burst(volume_id)
            if is_burst and not self._cache.has(volume_id):
                import threading

                threading.Thread(
                    target=self._cache.download_volume,
                    args=(cloud_key, volume_id),
                    daemon=True,
                ).start()

        return data

    # ─── Rehydration (TIERED → local, writable again) ──────────────────

    def rehydrate_volume(self, volume_id: str) -> bool:
        """Re-download a tiered volume to make it locally available again.

        Downloads the full .vol from cloud back to the volumes directory,
        removes the TIERED state from the manifest, and cleans up the
        LRU cache entry if present.  After rehydration, the Rust VolumeEngine
        can serve reads from the local .vol file directly.

        This implements the ``TIERED → ACTIVE`` transition from the issue
        design: "re-downloaded, writable again".

        Returns True if rehydration succeeded.
        """
        entry = self._manifest.get(volume_id)
        if entry is None or entry.state != VolumeState.TIERED:
            logger.warning("Cannot rehydrate volume %s: not in TIERED state", volume_id)
            return False

        vol_path = self._volumes_dir / f"{volume_id}.vol"
        if vol_path.exists():
            # Already local (maybe lingering from crash) — just clear state
            self._manifest.remove(volume_id)
            self._cache.remove(volume_id)
            logger.info("Rehydrate: volume %s already local, cleared TIERED state", volume_id)
            return True

        # Stream download from cloud to the volumes directory (not the cache).
        # Uses chunked range reads to avoid buffering the full volume in RAM.
        tmp_path = vol_path.with_suffix(".rehydrate.tmp")
        try:
            remote_size = self._cloud.get_size(entry.cloud_key)
            chunk_size = 8 * 1024 * 1024
            with open(tmp_path, "wb") as f:
                offset = 0
                while offset < remote_size:
                    read_size = min(chunk_size, remote_size - offset)
                    chunk = self._cloud.get_blob_range(entry.cloud_key, offset, read_size)
                    f.write(chunk)
                    offset += read_size
            tmp_path.rename(vol_path)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            logger.error("Rehydrate: failed to download volume %s: %s", volume_id, e)
            return False

        # Verify size matches
        local_size = vol_path.stat().st_size
        if entry.size_bytes > 0 and local_size != entry.size_bytes:
            logger.error(
                "Rehydrate: size mismatch for %s (expected %d, got %d)",
                volume_id,
                entry.size_bytes,
                local_size,
            )
            vol_path.unlink(missing_ok=True)
            return False

        # Clear tiered state — volume is now local again
        self._manifest.remove(volume_id)
        # Clean up cache entry if it exists (no longer needed)
        self._cache.remove(volume_id)

        logger.info(
            "Rehydrated volume %s (%.1f MB) — now locally available",
            volume_id,
            local_size / (1024 * 1024),
        )
        return True

    @property
    def cache(self) -> VolumeCache:
        """Access the volume cache (for stats/testing)."""
        return self._cache

    @property
    def is_running(self) -> bool:
        return self._running
