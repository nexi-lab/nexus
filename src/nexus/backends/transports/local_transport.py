"""Local filesystem Transport — raw key→blob I/O on local disk.

Implements the Transport protocol using direct writes with optional
fsync for durability. CAS is idempotent (same hash = same bytes), so
temp+replace atomicity is unnecessary — direct write is safe and faster.

Storage mapping:
    key "cas/ab/cd/abcd1234…" → root_path / "cas" / "ab" / "cd" / "abcd1234…"

The transport has NO knowledge of CAS addressing — it maps raw string
keys to filesystem paths under root_path.

References:
    - Issue #1323: CAS x Backend orthogonal composition
    - Issue #1772: CAS local transport I/O optimization
    - transports/gcs_transport.py — reference transport implementation
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

logger = logging.getLogger(__name__)


class LocalTransport:
    """Raw key→blob I/O on local filesystem.

    Implements the Transport protocol (structural typing — no inheritance).

    Args:
        root_path: Root directory for all blob storage.
        fsync: Call fsync after writing content blobs for durability.
               Disable for test performance or battery-backed RAID.
    """

    transport_name: str = "local"

    def __init__(self, root_path: str | Path, *, fsync: bool = True) -> None:
        self._root = Path(root_path).resolve()
        self._fsync = fsync
        self._known_parents: set[str] = set()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Map a storage key to an absolute filesystem path."""
        return self._root / key

    def _ensure_parent(self, path: Path) -> None:
        """Ensure parent directory exists, with monotonic cache.

        CAS has at most 65,536 two-level dirs (cas/ab/cd/). Once created,
        they are never deleted during normal operation, so we cache the
        result. On ENOENT from os.open we evict and retry (see store).
        """
        parent = str(path.parent)
        if parent not in self._known_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._known_parents.add(parent)

    # === Transport Protocol Methods ===

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Direct write: raw fd → fsync → done. No temp+replace.

        CAS is idempotent (same hash = same bytes), so direct write is safe.
        Saves ~200μs per write by eliminating temp file + os.replace overhead.

        Returns None (local FS has no versioning).
        """
        path = self._resolve(key)
        self._ensure_parent(path)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        except FileNotFoundError:
            # Parent dir was deleted externally — evict cache and retry
            self._known_parents.discard(str(path.parent))
            self._ensure_parent(path)
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            if self._fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        return None

    def get_mtime(self, key: str) -> float:
        """Blob mtime as Unix timestamp. For GC age threshold."""
        path = self._resolve(key)
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            raise NexusFileNotFoundError(key) from None

    def store_nosync(self, key: str, data: bytes) -> None:
        """Direct write without fsync — for reconstructable metadata.

        CDC meta JSON is reconstructable (chunk/manifest flags for GC).
        Crash-losing the meta file means GC cannot identify CDC manifests,
        but the blob is still readable.
        """
        path = self._resolve(key)
        self._ensure_parent(path)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        path = self._resolve(key)
        try:
            return path.read_bytes(), None
        except (FileNotFoundError, IsADirectoryError):
            raise NexusFileNotFoundError(key) from None
        except OSError as e:
            raise BackendError(
                f"Failed to read blob at {key}: {e}",
                backend="local",
                path=key,
            ) from e

    def remove(self, key: str) -> None:
        path = self._resolve(key)
        if not path.exists():
            raise NexusFileNotFoundError(key)
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            # Clean up empty parent dirs up to root
            self._cleanup_empty_parents(path.parent)
        except FileNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except OSError as e:
            raise BackendError(
                f"Failed to delete blob at {key}: {e}",
                backend="local",
                path=key,
            ) from e

    def exists(self, key: str) -> bool:
        try:
            path = self._resolve(key)
            # For directory markers (keys ending with /), check dir existence
            if key.endswith("/"):
                return path.is_dir()
            return path.is_file()
        except Exception:
            return False

    def get_size(self, key: str) -> int:
        path = self._resolve(key)
        if not path.exists():
            raise NexusFileNotFoundError(key)
        try:
            return path.stat().st_size
        except OSError as e:
            raise BackendError(
                f"Failed to get blob size for {key}: {e}",
                backend="local",
                path=key,
            ) from e

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """S3-style listing with prefix and delimiter support.

        Returns (blob_keys, common_prefixes):
        - blob_keys: all blobs whose key starts with prefix (and, when
          delimiter is set, do NOT contain the delimiter after the prefix)
        - common_prefixes: unique prefix+<chars-up-to-delimiter>+delimiter
          for keys that DO contain the delimiter after the prefix
        """
        base = self._resolve(prefix)

        blob_keys: list[str] = []
        common_prefixes: set[str] = set()

        try:
            if not delimiter:
                # No delimiter — recursive listing of ALL blobs under prefix
                if base.is_dir():
                    for p in base.rglob("*"):
                        if p.is_file():
                            rel = str(p.relative_to(self._root))
                            blob_keys.append(rel)
                elif base.is_file():
                    rel = str(base.relative_to(self._root))
                    blob_keys.append(rel)
                return sorted(blob_keys), []

            # With delimiter — single-level listing (like S3)
            # prefix might point to a directory or be a partial key prefix
            if base.is_dir():
                scan_dir = base
            elif base.parent.is_dir():
                scan_dir = base.parent
            else:
                return [], []

            for entry in scan_dir.iterdir():
                entry_key = str(entry.relative_to(self._root))
                # Only include entries that start with the original prefix
                if not entry_key.startswith(prefix.rstrip("/")):
                    continue
                if entry.is_file():
                    blob_keys.append(entry_key)
                elif entry.is_dir():
                    common_prefixes.add(entry_key + "/")

        except OSError as e:
            raise BackendError(
                f"Failed to list blobs with prefix {prefix}: {e}",
                backend="local",
                path=prefix,
            ) from e

        return sorted(blob_keys), sorted(common_prefixes)

    def copy_key(self, src_key: str, dst_key: str) -> None:
        src_path = self._resolve(src_key)
        if not src_path.is_file():
            raise NexusFileNotFoundError(src_key)
        dst_path = self._resolve(dst_key)
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_path), str(dst_path))
        except OSError as e:
            raise BackendError(
                f"Failed to copy blob from {src_key} to {dst_key}: {e}",
                backend="local",
                path=src_key,
            ) from e

    def create_dir(self, key: str) -> None:
        """Create an empty file as a directory marker.

        For local filesystem, we create actual directories instead of
        empty marker files, matching S3/GCS semantics where a key ending
        with '/' represents a directory.
        """
        path = self._resolve(key)
        try:
            if key.endswith("/"):
                # Directory marker — create the directory itself
                path.mkdir(parents=True, exist_ok=True)
            else:
                # Non-directory marker — create an empty file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
        except OSError as e:
            raise BackendError(
                f"Failed to create directory marker at {key}: {e}",
                backend="local",
                path=key,
            ) from e

    def move(self, src_key: str, dst_key: str) -> None:
        """Atomic move (rename) of a blob or directory."""
        src_path = self._resolve(src_key)
        if not src_path.exists():
            raise NexusFileNotFoundError(src_key)
        dst_path = self._resolve(dst_key)
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(src_path), str(dst_path))
            # Clean up empty parent dirs of source
            self._cleanup_empty_parents(src_path.parent)
        except OSError as e:
            raise BackendError(
                f"Failed to move blob from {src_key} to {dst_key}: {e}",
                backend="local",
                path=src_key,
            ) from e

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """True streaming read from local filesystem — no full download needed."""
        path = self._resolve(key)
        if not path.is_file():
            raise NexusFileNotFoundError(key)
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except OSError as e:
            raise BackendError(
                f"Failed to stream blob from {key}: {e}",
                backend="local",
                path=key,
            ) from e

    def store_chunked(
        self,
        key: str,
        chunks: "Iterator[bytes]",
        content_type: str = "",  # noqa: ARG002 — local FS ignores MIME
    ) -> str | None:
        """Stream chunks to local filesystem via temp file + atomic replace."""
        import tempfile

        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as tmp:
                for chunk in chunks:
                    tmp.write(chunk)
                tmp_path = tmp.name
            os.replace(tmp_path, str(path))
        except Exception as e:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
            raise BackendError(
                f"Failed to write chunked blob to {key}: {e}",
                backend="local",
                path=key,
            ) from e
        return None

    def store_from_path(self, key: str, src_path: str | Path) -> str | None:
        """Atomic move: src_path → final blob path (no memory copy).

        Used by CASAddressingEngine.write_stream to avoid loading streamed content
        back into memory after hashing to a temp file.
        """
        path = self._resolve(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(src_path), str(path))
        except Exception as e:
            # Cleanup source on failure
            with contextlib.suppress(OSError):
                Path(src_path).unlink(missing_ok=True)
            raise BackendError(
                f"Failed to move blob to {key}: {e}",
                backend="local",
                path=key,
            ) from e
        return None

    # === Extended Methods (transport protocol extensions, Issue #3403) ===

    def list_content_hashes(self) -> list[tuple[str, float]]:
        """List all CAS content hashes with their mtime.

        Scans cas/ directory tree and returns (hash_hex, mtime) pairs.
        Used by GC for reachability scan and by Bloom filter for seeding.
        """
        cas_dir = self._root / "cas"
        if not cas_dir.is_dir():
            return []

        result: list[tuple[str, float]] = []
        for path in cas_dir.rglob("*"):
            if path.is_file() and path.suffix not in (".meta", ".lock"):
                try:
                    result.append((path.name, path.stat().st_mtime))
                except OSError:
                    continue
        return result

    def batch_fetch(self, keys: list[str]) -> dict[str, bytes | None]:
        """Batch read multiple blobs, using Rust parallel mmap."""
        if not keys:
            return {}

        from nexus._rust_compat import read_files_bulk as _read_files_bulk

        paths = [str(self._resolve(k)) for k in keys]
        disk_contents: dict[str, bytes | None] = _read_files_bulk(paths)
        result: dict[str, bytes | None] = {}
        for key, path in zip(keys, paths, strict=True):
            result[key] = disk_contents.get(path)
        return result

    # === Internal Helpers ===

    def _cleanup_empty_parents(self, dir_path: Path) -> None:
        """Remove empty parent directories up to root."""
        try:
            current = dir_path
            while current != self._root and current.exists():
                if not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
        except OSError:
            pass
