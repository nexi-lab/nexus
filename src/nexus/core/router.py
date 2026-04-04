"""Path routing for mapping virtual paths to storage backends.

PathRouter = read-only query engine over MountTable.  Routes virtual paths
to storage backends using longest-prefix matching with mount-level access
control (readonly, admin_only).  Zone-aware: ``route(path, zone_id=)``
canonicalizes to ``/{zone_id}/{path}`` internally for LPM against
zone-canonical mount keys.

PathRouter does NOT own mount data — MountTable does.  PathRouter reads
MountTable; DriverLifecycleCoordinator writes MountTable.

Architecture:
    path, zone_id → PathRouter.route() → canonicalize → LPM → (backend, backend_path)

Issue #3584.
"""

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import AccessDeniedError, InvalidPathError, PathNotMountedError
from nexus.core.mount_table import MountEntry, MountTable, canonicalize_path, extract_zone_id

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.protocols.vfs_router import MountInfo


# Re-export for backward compatibility
__all__ = [
    "PathRouter",
    "RouteResult",
    "PipeRouteResult",
    "StreamRouteResult",
    "ExternalRouteResult",
    "canonicalize_path",
    "extract_zone_id",
    "strip_zone_prefix",
    "MountEntry",
]


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


@dataclass
class RouteResult:
    """Result of path routing — dispatches to ObjectStoreABC backend + MetastoreABC."""

    backend: "ObjectStoreABC"
    metastore: "MetastoreABC"  # Zone's metadata store (per-zone Raft store)
    backend_path: str  # Path relative to backend root
    mount_point: str  # Matched mount point
    readonly: bool
    io_profile: str = "balanced"  # I/O tuning profile (Issue #1413)


@dataclass(frozen=True, slots=True)
class PipeRouteResult:
    """Route result for DT_PIPE — kernel dispatches to PipeManager."""

    path: str
    metastore: "MetastoreABC"


@dataclass(frozen=True, slots=True)
class StreamRouteResult:
    """Route result for DT_STREAM — kernel dispatches to StreamManager."""

    path: str
    metastore: "MetastoreABC"


@dataclass(frozen=True, slots=True)
class ExternalRouteResult:
    """Route result for DT_EXTERNAL_STORAGE — backend manages own namespace."""

    backend: "ObjectStoreABC"
    metastore: "MetastoreABC"
    backend_path: str
    mount_point: str
    readonly: bool
    io_profile: str = "balanced"


class PathRouter:
    """Read-only query engine over MountTable.

    Routes virtual paths to storage backends using longest-prefix matching
    with mount-level access control (readonly, admin_only).

    PathRouter does NOT own mount data.  MountTable is the single source
    of truth for mount entries, Rust LPM engine, and zone-canonical keys.
    PathRouter is a pure read-only consumer.

    Design Principles:
    1. **Longest Prefix Match**: most specific mount wins (deepest path).
    2. **Mount-level access control**: ``readonly`` and ``admin_only`` as mount options.
    3. **Rust-accelerated**: LPM + canonicalization in Rust (~30ns), Python fallback.

    Example Mounts::

        /workspace  → LocalFS (/var/nexus/workspace)
        /shared     → LocalFS (/var/nexus/shared)
        /system     → LocalFS (/var/nexus/system)  [readonly=True, admin_only=True]
        /external   → S3, GDrive, etc.

    Issue #3584.
    """

    def __init__(self, mount_table: "MountTable") -> None:
        """Initialize path router with a MountTable (read-only view).

        Args:
            mount_table: MountTable instance — the kernel mount table.
                Router reads from it; coordinator writes to it.
        """
        self._mount_table = mount_table
        # Keep metastore reference for DT_PIPE/DT_STREAM inode check in route()
        self._metastore: MetastoreABC = mount_table._default_metastore

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
                return PipeRouteResult(path=virtual_path, metastore=self._metastore)
            if meta.is_stream:
                return StreamRouteResult(path=virtual_path, metastore=self._metastore)

        # RUST_FALLBACK: route — LPM + canonicalize in single FFI call (~30ns)
        rust = self._mount_table.rust
        if rust is not None:
            try:
                rust_result = rust.route(virtual_path, zone_id, is_admin, check_write)
            except PermissionError as e:
                # Re-raise with user-facing path (strip zone prefix from Rust error)
                msg = str(e).replace(f"/{zone_id}/", "/").replace(f"/{zone_id}'", "/'")
                raise AccessDeniedError(msg) from None
            except ValueError:
                raise PathNotMountedError(virtual_path) from None
            entry = self._mount_table.get_canonical(rust_result.mount_point)
            if entry is None:
                raise PathNotMountedError(virtual_path)
            user_mp = extract_zone_id(rust_result.mount_point)[1]
            # Check file metadata first; fall back to mount-root metadata so
            # connector files (which have no per-file metadata) still route
            # through ExternalRouteResult when their mount root is DT_EXTERNAL_STORAGE.
            _route_meta = meta if meta is not None else self._metastore.get(user_mp)
            if _route_meta is not None and _route_meta.is_external_storage:
                return ExternalRouteResult(
                    backend=entry.backend,
                    metastore=entry.metastore,
                    backend_path=rust_result.backend_path,
                    mount_point=user_mp,
                    readonly=rust_result.readonly,
                    io_profile=rust_result.io_profile,
                )
            return RouteResult(
                backend=entry.backend,
                metastore=entry.metastore,
                backend_path=rust_result.backend_path,
                mount_point=user_mp,
                readonly=rust_result.readonly,
                io_profile=rust_result.io_profile,
            )

        # Python fallback: zone-canonical LPM via MountTable
        result = self._mount_table.lookup_lpm(virtual_path, zone_id)
        if result is None:
            raise PathNotMountedError(virtual_path)

        canonical_key, entry = result
        canonical = canonicalize_path(virtual_path, zone_id)
        user_mp = extract_zone_id(canonical_key)[1]
        if entry.admin_only and not is_admin:
            raise AccessDeniedError(f"Mount '{user_mp}' requires admin privileges")
        if entry.readonly and check_write:
            raise AccessDeniedError(f"Mount '{user_mp}' is read-only")

        backend_path = self._strip_mount_prefix(canonical, canonical_key)

        # Check file metadata first; fall back to mount-root metadata so
        # connector files (which have no per-file metadata) still route
        # through ExternalRouteResult when their mount root is DT_EXTERNAL_STORAGE.
        _route_meta = meta if meta is not None else self._metastore.get(user_mp)
        if _route_meta is not None and _route_meta.is_external_storage:
            return ExternalRouteResult(
                backend=entry.backend,
                metastore=entry.metastore,
                backend_path=backend_path,
                mount_point=user_mp,
                readonly=entry.readonly,
                io_profile=entry.io_profile,
            )
        return RouteResult(
            backend=entry.backend,
            metastore=entry.metastore,
            backend_path=backend_path,
            mount_point=user_mp,
            readonly=entry.readonly,
            io_profile=entry.io_profile,
        )

    # ------------------------------------------------------------------
    # Mount table queries (delegate to MountTable)
    # ------------------------------------------------------------------

    def get_mount_points(self) -> list[str]:
        """Return all active mount point paths (user-facing, no zone prefix)."""
        return self._mount_table.mount_points()

    def has_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Check if an active mount exists at the given mount point."""
        return self._mount_table.has(mount_point, zone_id)

    def get_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> "MountInfo | None":
        """Get mount info for a specific mount point, or None if not found."""
        from nexus.core.protocols.vfs_router import MountInfo

        entry = self._mount_table.get(mount_point, zone_id)
        if entry is None:
            return None
        from nexus.core.path_utils import normalize_path

        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return None
        return MountInfo(
            mount_point=normalized,
            readonly=entry.readonly,
            admin_only=entry.admin_only,
            backend=entry.backend,
        )

    def get_mount_entry_for_path(
        self, path: str, zone_id: str = ROOT_ZONE_ID
    ) -> "MountEntry | None":
        """Find the mount entry covering *path* via longest-prefix match.

        Returns the ``MountEntry`` (includes stream_backend_factory).
        For public mount info, use ``get_mount()`` instead.
        """
        result = self._mount_table.lookup_lpm(path, zone_id)
        return result[1] if result is not None else None

    def list_mounts(self) -> "list[MountInfo]":
        """List all active mounts."""
        from nexus.core.protocols.vfs_router import MountInfo

        return sorted(
            [
                MountInfo(
                    mount_point=extract_zone_id(canonical_key)[1],
                    readonly=entry.readonly,
                    admin_only=entry.admin_only,
                    backend=entry.backend,
                )
                for canonical_key, entry in self._mount_table.items()
            ],
            key=lambda m: m.mount_point,
        )

    def get_backend_by_name(self, name: str) -> "ObjectStoreABC | None":
        """Look up backend by name.

        Useful for operations that need a specific backend
        (e.g., CLI undo needs to read from the backend that stored content).
        """
        for _, entry in self._mount_table.items():
            if entry.backend.name == name:
                return entry.backend
        return None

    # ------------------------------------------------------------------
    # Path validation and normalization (kept -- security-critical)
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

            ("/workspace/data/file.txt", "/workspace") -> "data/file.txt"
            ("/workspace", "/workspace") -> ""
            ("/shared/docs/report.pdf", "/shared") -> "docs/report.pdf"
            ("/workspace/data/file.txt", "/") -> "workspace/data/file.txt"
        """
        if virtual_path == mount_point:
            return ""
        if mount_point == "/":
            return virtual_path.lstrip("/")
        return virtual_path[len(mount_point) :].lstrip("/")
