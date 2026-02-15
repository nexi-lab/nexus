"""Lock-free Content-Addressable Storage (CAS) blob store (Issue #925).

Eliminates file-based lock contention (FileLock) from CAS operations.

Blob writes are fully lock-free and idempotent: same content always
produces the same hash and path, so concurrent writers overwrite each
other harmlessly.

Metadata ref_count uses a lightweight in-memory stripe lock (no disk I/O)
to coordinate the read-modify-write cycle. This replaces the expensive
FileLock that previously held a disk lock during the entire blob write +
metadata update cycle.

Architecture:
    CASBlobStore
    ├── write_blob()    — lock-free idempotent blob write (with fsync)
    ├── read_blob()     — direct read with retry
    ├── blob_exists()   — existence check
    ├── read_meta()     — metadata read with retry
    ├── write_meta()    — atomic temp+replace metadata write (no fsync)
    ├── store()         — blob write + ref_count increment (meta-locked)
    ├── release()       — ref_count decrement, delete at zero (meta-locked)
    └── cleanup_empty_dirs() — remove empty parent dirs up to cas_root
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from nexus.core.exceptions import BackendError

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CASMeta — frozen metadata container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CASMeta:
    """Immutable metadata for a CAS entry.

    Attributes:
        ref_count: Number of references to this blob.
        size: Content size in bytes.
        extra: Tuple of (key, value) pairs for unknown/extension fields.
               Using tuple-of-tuples keeps the dataclass truly frozen.
    """

    ref_count: int = 0
    size: int = 0
    extra: tuple[tuple[str, Any], ...] = ()

    # -- Serialization (backward-compatible with existing JSON format) ------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict, preserving extra fields."""
        d: dict[str, Any] = {"ref_count": self.ref_count, "size": self.size}
        for k, v in self.extra:
            d[k] = v
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CASMeta:
        """Deserialize from a dict, capturing unknown keys in *extra*."""
        ref_count = int(data.get("ref_count", 0))
        size = int(data.get("size", 0))
        extra = tuple((k, v) for k, v in data.items() if k not in ("ref_count", "size"))
        return cls(ref_count=ref_count, size=size, extra=extra)

    def inc_ref(self) -> CASMeta:
        """Return a new CASMeta with ref_count incremented by 1."""
        return CASMeta(ref_count=self.ref_count + 1, size=self.size, extra=self.extra)

    def dec_ref(self) -> CASMeta:
        """Return a new CASMeta with ref_count decremented by 1 (min 0)."""
        return CASMeta(ref_count=max(0, self.ref_count - 1), size=self.size, extra=self.extra)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def cas_retry(
    fn: Callable[[], _T],
    *,
    max_attempts: int = 10,
    base_delay: float = 0.001,
    retryable: tuple[type[Exception], ...] = (json.JSONDecodeError, OSError, PermissionError),
) -> _T:
    """Call *fn()* with exponential backoff + jitter on retryable errors.

    Args:
        fn: Zero-argument callable to invoke.
        max_attempts: Maximum number of attempts.
        base_delay: Base delay in seconds (doubles each attempt).
        retryable: Exception types that trigger a retry.

    Returns:
        The return value of *fn()*.

    Raises:
        The last exception if all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except retryable as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                time.sleep(delay)
    assert last_exc is not None  # noqa: S101 — guaranteed by max_attempts >= 1
    raise last_exc


# ---------------------------------------------------------------------------
# Stripe lock — lightweight in-memory coordination for metadata updates
# ---------------------------------------------------------------------------

_NUM_STRIPES = 64  # power of 2 for fast modulo


class _StripeLock:
    """Fixed-size array of threading.Lock objects indexed by hash.

    Provides per-hash coordination for metadata read-modify-write cycles
    without any disk I/O. Much cheaper than FileLock (~μs vs ~ms).
    """

    __slots__ = ("_locks",)

    def __init__(self, num_stripes: int = _NUM_STRIPES) -> None:
        self._locks = [threading.Lock() for _ in range(num_stripes)]

    def acquire_for(self, content_hash: str) -> threading.Lock:
        """Return the stripe lock for a given content hash (not acquired)."""
        # Use last 4 hex chars for even distribution
        idx = int(content_hash[-4:], 16) % len(self._locks)
        return self._locks[idx]


# ---------------------------------------------------------------------------
# CASBlobStore
# ---------------------------------------------------------------------------


class CASBlobStore:
    """CAS engine with lock-free blob writes and striped metadata locks.

    Blob writes are fully idempotent (same content -> same path).
    Metadata updates use a lightweight in-memory stripe lock to coordinate
    the read-modify-write cycle on ref_count. No FileLock or disk-based
    locks are used.

    Args:
        cas_root: Root directory for CAS storage (e.g. ``<root>/cas``).
        fsync_blobs: Call fsync after writing content blobs (default True).
                     Disable for high-throughput scenarios on battery-backed RAID.
    """

    __slots__ = ("cas_root", "_fsync_blobs", "_meta_locks")

    def __init__(self, cas_root: Path, *, fsync_blobs: bool = True) -> None:
        self.cas_root = cas_root
        self._fsync_blobs = fsync_blobs
        self._meta_locks = _StripeLock()

    # -- Path utilities -----------------------------------------------------

    def hash_to_path(self, content_hash: str) -> Path:
        """Convert content hash to a two-level directory CAS path.

        Layout: ``cas/<hash[0:2]>/<hash[2:4]>/<hash>``
        """
        if len(content_hash) < 4:
            raise ValueError(f"Invalid hash length: {content_hash}")
        return self.cas_root / content_hash[:2] / content_hash[2:4] / content_hash

    def meta_path(self, content_hash: str) -> Path:
        """Return the ``.meta`` sidecar path for *content_hash*."""
        return self.hash_to_path(content_hash).with_suffix(".meta")

    @contextlib.contextmanager
    def meta_lock(self, content_hash: str) -> Iterator[None]:
        """Acquire the stripe lock for *content_hash* as a context manager.

        Use this when external code needs coordinated metadata access
        (e.g. chunked manifest updates in ChunkedStorageMixin).
        """
        lock = self._meta_locks.acquire_for(content_hash)
        with lock:
            yield

    # -- Metadata I/O -------------------------------------------------------

    def read_meta(self, content_hash: str) -> CASMeta:
        """Read metadata with retry for transient I/O errors.

        Returns a default ``CASMeta(ref_count=0, size=0)`` when the
        ``.meta`` file does not exist.
        """
        mp = self.meta_path(content_hash)

        def _read() -> CASMeta:
            if not mp.exists():
                return CASMeta()
            text = mp.read_text(encoding="utf-8")
            return CASMeta.from_dict(json.loads(text))

        try:
            return cas_retry(_read)
        except (json.JSONDecodeError, OSError) as exc:
            raise BackendError(
                f"Failed to read metadata: {exc}: {content_hash}",
                backend="local",
                path=content_hash,
            ) from exc

    def write_meta(self, content_hash: str, meta: CASMeta) -> None:
        """Atomically write metadata via temp file + os.replace.

        No fsync for .meta — these files are reconstructible from the
        metadata store references.
        """
        mp = self.meta_path(content_hash)

        def _write() -> None:
            mp.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=mp.parent,
                    delete=False,
                    suffix=".tmp",
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(json.dumps(meta.to_dict()))
                    tmp.flush()
                    # No os.fsync — .meta is reconstructible
                os.replace(str(tmp_path), str(mp))
                tmp_path = None  # replaced successfully
            except BaseException:
                if tmp_path is not None and tmp_path.exists():
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                raise

        try:
            cas_retry(_write, retryable=(PermissionError,))
        except OSError as exc:
            raise BackendError(
                f"Failed to write metadata: {exc}: {content_hash}",
                backend="local",
                path=content_hash,
            ) from exc

    # -- Blob I/O -----------------------------------------------------------

    def write_blob(self, content_hash: str, content: bytes) -> bool:
        """Idempotent blob write with fsync for durability.

        If the blob already exists on disk, this is a no-op.

        Args:
            content_hash: Pre-computed hash of *content*.
            content: Raw bytes to store.

        Returns:
            True if a new blob was written, False if it already existed.
        """
        blob_path = self.hash_to_path(content_hash)
        if blob_path.exists():
            return False

        blob_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=blob_path.parent, delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(content)
                tmp.flush()
                if self._fsync_blobs:
                    os.fsync(tmp.fileno())
            os.replace(str(tmp_path), str(blob_path))
            tmp_path = None
            return True
        finally:
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def read_blob(self, content_hash: str, *, verify: bool = False) -> bytes:
        """Read blob content with retry for transient I/O errors.

        Args:
            content_hash: Hash identifying the blob.
            verify: If True, recompute the hash and raise on mismatch.

        Raises:
            BackendError: If the blob cannot be read after retries
                          or if hash verification fails.
        """
        blob_path = self.hash_to_path(content_hash)

        def _read() -> bytes:
            return blob_path.read_bytes()

        try:
            content = cas_retry(
                _read,
                max_attempts=3,
                base_delay=0.01,
                retryable=(OSError,),
            )
        except OSError as exc:
            raise BackendError(
                f"Failed to read blob: {exc}: {content_hash}",
                backend="local",
                path=content_hash,
            ) from exc

        if verify:
            from nexus.core.hash_fast import hash_content

            actual_hash = hash_content(content)
            if actual_hash != content_hash:
                raise BackendError(
                    f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                    backend="local",
                    path=content_hash,
                )

        return content

    def blob_exists(self, content_hash: str) -> bool:
        """Check if a blob exists on disk."""
        return self.hash_to_path(content_hash).exists()

    # -- High-level operations ----------------------------------------------

    def store(
        self,
        content_hash: str,
        content: bytes,
        *,
        extra_meta: dict[str, Any] | None = None,
    ) -> bool:
        """Write blob + increment ref_count.

        This is the primary write entry point. It:
        1. Writes the blob idempotently (lock-free, no coordination).
        2. Under a lightweight in-memory stripe lock, reads current
           metadata, increments ref_count, and writes it back.

        Args:
            content_hash: Pre-computed hash of *content*.
            content: Raw bytes to store.
            extra_meta: Additional metadata fields (e.g. is_chunk).

        Returns:
            True if a new blob was written, False if it already existed.
        """
        # Step 1: lock-free blob write (idempotent)
        self.write_blob(content_hash, content)

        # Step 2: coordinated metadata update (stripe lock)
        # Always read-then-increment inside lock to handle the case where
        # multiple threads race past write_blob before any metadata exists.
        lock = self._meta_locks.acquire_for(content_hash)
        with lock:
            meta = self.read_meta(content_hash)
            if meta.ref_count == 0 and meta.size == 0 and not meta.extra:
                # First metadata write for this blob
                extra: tuple[tuple[str, Any], ...] = ()
                if extra_meta:
                    extra = tuple(extra_meta.items())
                meta = CASMeta(ref_count=1, size=len(content), extra=extra)
            else:
                meta = meta.inc_ref()
            self.write_meta(content_hash, meta)
            is_new = meta.ref_count == 1

        return is_new

    def release(self, content_hash: str) -> bool:
        """Decrement ref_count; delete blob + meta when it reaches zero.

        Args:
            content_hash: Hash of the content to release.

        Returns:
            True if the blob was deleted (ref_count reached 0),
            False if only the ref_count was decremented.
        """
        lock = self._meta_locks.acquire_for(content_hash)
        with lock:
            meta = self.read_meta(content_hash)

            if meta.ref_count <= 1:
                blob_path = self.hash_to_path(content_hash)
                mp = self.meta_path(content_hash)

                with contextlib.suppress(FileNotFoundError):
                    blob_path.unlink()
                with contextlib.suppress(FileNotFoundError):
                    mp.unlink()

                self.cleanup_empty_dirs(blob_path.parent)
                return True

            self.write_meta(content_hash, meta.dec_ref())
            return False

    def cleanup_empty_dirs(self, dir_path: Path) -> None:
        """Remove empty parent directories up to *cas_root*."""
        try:
            current = dir_path
            while current != self.cas_root and current.exists():
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
        except OSError:
            pass
