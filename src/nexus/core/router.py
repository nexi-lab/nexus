"""Path routing for mapping virtual paths to storage backends.

PathRouter = Linux VFS mount table.  Routes virtual paths to storage backends
using longest-prefix matching with mount-level access control (readonly,
admin_only).  Zone-aware: ``route(path, zone_id=)`` canonicalizes to
``/{zone_id}/{path}`` internally for LPM against zone-canonical mount keys.

PathRouter is a pure in-memory routing table (like Linux VFS ``vfsmount``).
DT_MOUNT persistence in the metastore is a separate concern owned by the
mount subsystem (``MountService``, ``ZoneManager.mount``,
``ensure_topology``).

Architecture:
    path, zone_id → PathRouter.route() → canonicalize → LPM → (backend, backend_path)
"""

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import AccessDeniedError, InvalidPathError, PathNotMountedError

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.protocols.vfs_router import MountInfo


# ---------------------------------------------------------------------------
# Zone-canonical path helpers (pure functions, ~0 cost)
# ---------------------------------------------------------------------------


def canonicalize_path(path: str, zone_id: str = ROOT_ZONE_ID) -> str:
    """Canonicalize a virtual path with zone prefix for routing.

    ``canonicalize_path("/workspace/file.txt", "root")``
    → ``"/root/workspace/file.txt"``
    """
    stripped = path.lstrip("/")
    return f"/{zone_id}/{stripped}" if stripped else f"/{zone_id}"


def strip_zone_prefix(canonical_path: str, zone_id: str) -> str:
    """Strip zone prefix from canonical path to get metastore-relative path.

    ``strip_zone_prefix("/root/workspace/file.txt", "root")``
    → ``"/workspace/file.txt"``
    """
    prefix = f"/{zone_id}"
    if canonical_path == prefix:
        return "/"
    if canonical_path.startswith(prefix + "/"):
        return canonical_path[len(prefix) :]
    return canonical_path


def extract_zone_id(canonical_path: str) -> tuple[str, str]:
    """Extract (zone_id, relative_path) from a canonical path.

    ``extract_zone_id("/root/workspace/file.txt")``
    → ``("root", "/workspace/file.txt")``
    """
    parts = canonical_path.lstrip("/").split("/", 1)
    zone_id = parts[0]
    relative = "/" + parts[1] if len(parts) > 1 else "/"
    return zone_id, relative


@dataclass(frozen=True, slots=True)
class _MountEntry:
    """Runtime mount entry — holds Python objects that cannot be serialized.

    The ``backend`` field is typed ``ObjectStoreABC`` — the kernel's file
    operations contract.  PathRouter stores and returns it; the caller
    (NexusFS) invokes CAS / directory methods directly.  Like
    Linux ``struct super_block *`` in the mount table.
    """

    backend: "ObjectStoreABC"
    readonly: bool
    admin_only: bool
    io_profile: str
    stream_backend_factory: Any = None  # Callable[[str, int], StreamBackend] | None


@dataclass
class RouteResult:
    """Result of path routing — dispatches to ObjectStoreABC backend."""

    backend: "ObjectStoreABC"
    backend_path: str  # Path relative to backend root
    mount_point: str  # Matched mount point
    readonly: bool
    io_profile: str = "balanced"  # I/O tuning profile (Issue #1413)


@dataclass(frozen=True, slots=True)
class PipeRouteResult:
    """Route result for DT_PIPE — kernel dispatches to PipeManager.

    Like Linux VFS dispatching to ``fifo_fops`` when ``S_ISFIFO``
    on inode lookup.  Callers (sys_read, sys_write, sys_unlink)
    check ``isinstance`` and dispatch to PipeManager.
    """

    path: str


@dataclass(frozen=True, slots=True)
class StreamRouteResult:
    """Route result for DT_STREAM — kernel dispatches to StreamManager.

    Like ``PipeRouteResult`` but for append-only streams with
    non-destructive offset-based reads.
    """

    path: str


@dataclass(frozen=True, slots=True)
class ExternalRouteResult:
    """Route result for DT_EXTERNAL_STORAGE — backend manages own namespace.

    Like ``PipeRouteResult`` for DT_PIPE, but for backends whose content lives
    outside kernel-managed storage (OAuth connectors, CLI connectors, APIs).
    Kernel skips metastore lookup and dispatches directly to backend methods.
    """

    backend: "ObjectStoreABC"
    backend_path: str
    mount_point: str
    readonly: bool
    io_profile: str = "balanced"


# ---------------------------------------------------------------------------
# Rust acceleration (try import, fallback to Python)
# ---------------------------------------------------------------------------

try:
    from nexus_fast import RustPathRouter as _RustRouter  # type: ignore[import-untyped]

    _HAS_RUST_ROUTER = True
except ImportError:
    _HAS_RUST_ROUTER = False


class PathRouter:
    """Route virtual paths to storage backends using mount table.

    Design Principles:
    1. **Longest Prefix Match**: most specific mount wins (deepest path).
    2. **Mount-level access control**: ``readonly`` and ``admin_only`` as mount options.
    3. **Rust-accelerated**: LPM + canonicalization in Rust (~30ns), Python fallback.

    Example Mounts::

        /workspace  → LocalFS (/var/nexus/workspace)
        /shared     → LocalFS (/var/nexus/shared)
        /system     → LocalFS (/var/nexus/system)  [readonly=True, admin_only=True]
        /external   → S3, GDrive, etc.
    """

    def __init__(self, metastore: "MetastoreABC") -> None:
        """Initialize path router with metastore-backed mount table.

        Args:
            metastore: MetastoreABC instance (the kernel's inode layer).
        """
        self._metastore = metastore
        self._backends: dict[str, _MountEntry] = {}
        self._rust: _RustRouter | None = _RustRouter() if _HAS_RUST_ROUTER else None

    def add_mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
        stream_backend_factory: Any = None,
        zone_id: str = ROOT_ZONE_ID,
    ) -> None:
        """Register a backend at *mount_point* for path routing.

        Pure in-memory operation (like Linux VFS ``vfsmount`` insertion).
        Mount key is zone-canonical: ``/{zone_id}/{mount_point}`` so that
        LPM naturally distinguishes zones.

        Args:
            mount_point: Virtual path prefix (must start with /).
            backend: ObjectStoreABC instance (kernel file_operations contract).
            readonly: Whether mount is readonly.
            admin_only: Whether mount requires admin privileges.
            io_profile: I/O tuning profile.
            stream_backend_factory: Optional callable ``(path, capacity) -> StreamBackend``
                for creating DT_STREAM with non-default backing store (e.g. CAS, WAL).
            zone_id: Zone for this mount (default ROOT_ZONE_ID).

        Raises:
            ValueError: If mount_point is invalid.
        """
        mount_point = self._normalize_path(mount_point)
        canonical_key = canonicalize_path(mount_point, zone_id)
        self._register_mount_entry(
            canonical_key,
            backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
            stream_backend_factory=stream_backend_factory,
        )
        if self._rust is not None:
            self._rust.add_mount(mount_point, zone_id, readonly, admin_only, io_profile)

    def _register_mount_entry(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool,
        admin_only: bool,
        io_profile: str,
        stream_backend_factory: Any = None,
    ) -> None:
        """Register the runtime mount entry for path routing."""
        self._backends[mount_point] = _MountEntry(
            backend=backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
            stream_backend_factory=stream_backend_factory,
        )

    def route(
        self,
        virtual_path: str,
        *,
        is_admin: bool = False,
        check_write: bool = False,
        zone_id: str = ROOT_ZONE_ID,
    ) -> RouteResult | PipeRouteResult | StreamRouteResult | ExternalRouteResult:
        """Route virtual path to backend with mount-level access control.

        Zone-aware: canonicalizes ``virtual_path`` with ``zone_id`` prefix
        internally, then performs LPM against zone-canonical mount keys.
        Callers pass ``route(path, zone_id=self._zone_id)``.

        DT_PIPE / DT_STREAM inodes are detected at the exact target path
        (first iteration) and short-circuit to ``PipeRouteResult`` /
        ``StreamRouteResult`` — like Linux VFS dispatching to special
        ``fops`` when the inode type matches.

        Args:
            virtual_path: Virtual path to route.
            is_admin: Whether requester has admin privileges.
            check_write: Whether to check write permissions.

        Returns:
            RouteResult for regular files, PipeRouteResult for DT_PIPE,
            StreamRouteResult for DT_STREAM, ExternalRouteResult for
            DT_EXTERNAL_STORAGE.

        Raises:
            PathNotMountedError: No mount found for path.
            AccessDeniedError: Access denied by mount-level rules.
            InvalidPathError: Path validation failed.
        """
        virtual_path = self.validate_path(virtual_path)

        # DT_PIPE / DT_STREAM: kernel-native IPC dispatch at exact target
        # path.  IPC inodes are endpoints (not prefixes), checked before
        # mount LPM.  This is the only metastore.get() in route().
        meta = self._metastore.get(virtual_path)
        if meta is not None:
            if meta.is_pipe:
                return PipeRouteResult(path=virtual_path)
            if meta.is_stream:
                return StreamRouteResult(path=virtual_path)

        # Rust fast path: LPM + canonicalize in single FFI call (~30ns)
        if self._rust is not None:
            rust_result = self._rust.route(virtual_path, zone_id, is_admin, check_write)
            entry = self._backends.get(rust_result.mount_point)
            if entry is None:
                raise PathNotMountedError(virtual_path)
            if meta is not None and meta.is_external_storage:
                return ExternalRouteResult(
                    backend=entry.backend,
                    backend_path=rust_result.backend_path,
                    mount_point=rust_result.mount_point,
                    readonly=rust_result.readonly,
                    io_profile=rust_result.io_profile,
                )
            return RouteResult(
                backend=entry.backend,
                backend_path=rust_result.backend_path,
                mount_point=rust_result.mount_point,
                readonly=rust_result.readonly,
                io_profile=rust_result.io_profile,
            )

        # Python fallback: zone-canonical LPM using _backends dict
        canonical = canonicalize_path(virtual_path, zone_id)
        current = canonical
        while True:
            entry = self._backends.get(current)
            if entry is not None:
                if entry.admin_only and not is_admin:
                    raise AccessDeniedError(f"Mount '{current}' requires admin privileges")
                if entry.readonly and check_write:
                    raise AccessDeniedError(f"Mount '{current}' is read-only")

                backend_path = self._strip_mount_prefix(canonical, current)

                if meta is not None and meta.is_external_storage:
                    return ExternalRouteResult(
                        backend=entry.backend,
                        backend_path=backend_path,
                        mount_point=current,
                        readonly=entry.readonly,
                        io_profile=entry.io_profile,
                    )
                return RouteResult(
                    backend=entry.backend,
                    backend_path=backend_path,
                    mount_point=current,
                    readonly=entry.readonly,
                    io_profile=entry.io_profile,
                )

            if current == "/":
                break
            current = posixpath.dirname(current)

        raise PathNotMountedError(virtual_path)

    # ------------------------------------------------------------------
    # Mount table queries
    # ------------------------------------------------------------------

    def get_mount_points(self) -> list[str]:
        """Return all active mount point paths.

        Returns paths from the ``_backends`` registry (active mounts with
        loaded backends). DT_MOUNT entries in metastore without a loaded
        backend are excluded (stale/remote mounts).
        """
        return sorted(self._backends.keys())

    def has_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Check if an active mount exists at the given mount point."""
        try:
            normalized = self._normalize_path(mount_point)
            canonical = canonicalize_path(normalized, zone_id)
            return canonical in self._backends
        except ValueError:
            return False

    def get_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> "MountInfo | None":
        """Get mount info for a specific mount point, or None if not found."""
        from nexus.core.protocols.vfs_router import MountInfo

        try:
            normalized = self._normalize_path(mount_point)
            canonical = canonicalize_path(normalized, zone_id)
            entry = self._backends.get(canonical)
            if entry is None:
                return None
            return MountInfo(
                mount_point=canonical,
                readonly=entry.readonly,
                admin_only=entry.admin_only,
                backend=entry.backend,
            )
        except ValueError:
            return None

    def get_mount_entry_for_path(
        self, path: str, zone_id: str = ROOT_ZONE_ID
    ) -> "_MountEntry | None":
        """Find the mount entry covering *path* via longest-prefix match.

        Returns the raw ``_MountEntry`` (internal, includes stream_backend_factory).
        For public mount info, use ``get_mount()`` instead.
        """
        current = canonicalize_path(path, zone_id)
        while True:
            entry = self._backends.get(current)
            if entry is not None:
                return entry
            if current == "/":
                return None
            current = current.rsplit("/", 1)[0] or "/"

    def remove_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Remove a mount from the in-memory routing table.

        Pure in-memory operation.  DT_MOUNT cleanup in the metastore is
        the caller's responsibility (MountService, ZoneManager, etc.).

        Returns:
            True if mount was removed, False if not found.
        """
        try:
            normalized = self._normalize_path(mount_point)
            canonical = canonicalize_path(normalized, zone_id)
            if canonical in self._backends:
                del self._backends[canonical]
                if self._rust is not None:
                    self._rust.remove_mount(normalized, zone_id)
                return True
            return False
        except ValueError:
            return False

    def list_mounts(self) -> "list[MountInfo]":
        """List all active mounts."""
        from nexus.core.protocols.vfs_router import MountInfo

        return sorted(
            [
                MountInfo(
                    mount_point=mp,
                    readonly=entry.readonly,
                    admin_only=entry.admin_only,
                    backend=entry.backend,
                )
                for mp, entry in self._backends.items()
            ],
            key=lambda m: m.mount_point,
        )

    def get_backend_by_name(self, name: str) -> "ObjectStoreABC | None":
        """Look up backend by name.

        Useful for operations that need a specific backend
        (e.g., CLI undo needs to read from the backend that stored content).
        """
        for entry in self._backends.values():
            if entry.backend.name == name:
                return entry.backend
        return None

    # ------------------------------------------------------------------
    # Path validation and normalization (kept — security-critical)
    # ------------------------------------------------------------------

    def validate_path(self, path: str) -> str:
        """Validate path format and check for security issues.

        Rules:
        - Must start with ``/``
        - No null bytes or control characters
        - No path traversal (..)
        """
        if not path.startswith("/"):
            raise InvalidPathError(path, "Path must be absolute")

        if "\0" in path:
            raise InvalidPathError(path, "Path contains null byte")

        if any(ord(c) < 32 for c in path):
            raise InvalidPathError(path, "Path contains control characters")

        # SECURITY: Normalize BEFORE checking for path traversal
        normalized = self._normalize_path(path)

        if not normalized.startswith("/"):
            raise InvalidPathError(path, "Path traversal detected")

        # Detect if path traversal changed the top-level component
        if ".." in path:
            orig_parts = path.lstrip("/").split("/", 1)
            norm_parts = normalized.lstrip("/").split("/", 1)

            if len(orig_parts) > 0 and len(norm_parts) > 0:
                orig_top = orig_parts[0]
                norm_top = norm_parts[0]
                if orig_top != norm_top or norm_top == "":
                    raise InvalidPathError(
                        path, "Path traversal detected (attempted to escape mount boundary)"
                    )

        return normalized

    def _normalize_path(self, path: str) -> str:
        """Normalize virtual path.

        Rules: absolute, collapse ``//``, remove trailing ``/``, resolve ``.`` and ``..``.
        """
        if not path.startswith("/"):
            raise ValueError(f"Path must be absolute: {path}")

        normalized = posixpath.normpath(path)

        if not normalized.startswith("/"):
            raise ValueError(f"Path traversal detected: {path}")

        return normalized

    def _strip_mount_prefix(self, virtual_path: str, mount_point: str) -> str:
        """Strip mount prefix to get backend-relative path.

        Examples::

            ("/workspace/data/file.txt", "/workspace") → "data/file.txt"
            ("/workspace", "/workspace") → ""
            ("/shared/docs/report.pdf", "/shared") → "docs/report.pdf"
            ("/workspace/data/file.txt", "/") → "workspace/data/file.txt"
        """
        if virtual_path == mount_point:
            return ""
        if mount_point == "/":
            return virtual_path.lstrip("/")
        return virtual_path[len(mount_point) :].lstrip("/")
