"""Path routing for mapping virtual paths to storage backends.

PathRouter = read-only query engine over the kernel mount table. Routes
virtual paths to storage backends using longest-prefix matching with
mount-level access control (readonly, admin_only). Zone-aware.

Routing happens inside the Rust kernel (``PyKernel.route``). PathRouter
is a thin Python façade that:
  1. Enforces Python-side path validation and security checks.
  2. Calls ``kernel.route(path, zone_id, is_admin, check_write)``.
  3. Enriches the result with the Python-only ``_PyMountInfo`` (connector
     backend ref, stream backend factory) owned by the
     ``DriverLifecycleCoordinator``.
  4. Returns a ``RouteResult`` / ``PipeRouteResult`` / ``StreamRouteResult``
     / ``ExternalRouteResult`` to Python callers in ``NexusFS``.

The kernel is the single source of truth for mount state. PathRouter does
not own a ``MountTable``; it reads from the DLC and delegates to the
kernel for LPM and access checks.

Issue #3584.
"""

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import AccessDeniedError, InvalidPathError, PathNotMountedError
from nexus.core.path_utils import canonicalize_path, extract_zone_id, strip_zone_prefix

if TYPE_CHECKING:
    from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator, _PyMountInfo
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.protocols.vfs_router import MountInfo


__all__ = [
    "PathRouter",
    "RouteResult",
    "PipeRouteResult",
    "StreamRouteResult",
    "ExternalRouteResult",
    "canonicalize_path",
    "extract_zone_id",
    "strip_zone_prefix",
]


@dataclass
class RouteResult:
    """Result of path routing — dispatches to ObjectStoreABC backend + MetastoreABC."""

    backend: "ObjectStoreABC"
    metastore: "MetastoreABC"  # Default Python metastore (kernel owns per-mount)
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
    """Read-only query engine over the kernel mount table.

    PathRouter does NOT own mount data. The Rust kernel is the single
    source of truth for mount entries and LPM routing. PathRouter is a
    pure read-only consumer that enriches kernel route results with
    Python-side references held by the ``DriverLifecycleCoordinator``.

    Design principles:
    1. **Longest prefix match**: most specific mount wins (kernel LPM).
    2. **Mount-level access control**: ``readonly`` and ``admin_only``.
    3. **Kernel-native**: LPM + canonicalization inside Rust kernel.

    Issue #3584.
    """

    def __init__(
        self,
        dlc: "DriverLifecycleCoordinator",
        metastore: "MetastoreABC",
        kernel: Any,
    ) -> None:
        """Initialize path router.

        Args:
            dlc: DriverLifecycleCoordinator — owns the Python-side mount
                map and is written by ``mount()``/``unmount()``.
            metastore: Default Python metastore for DT_PIPE/DT_STREAM
                inode checks and fallback route result population.
            kernel: ``PyKernel`` instance (when Rust is available).
                Route() delegates to ``kernel.route(...)``; a minimal
                Python fallback handles the kernel-less case.
        """
        self._dlc = dlc
        self._metastore: MetastoreABC = metastore
        self._kernel: Any = kernel

    def route(
        self,
        virtual_path: str,
        *,
        is_admin: bool = False,
        check_write: bool = False,
        zone_id: str = ROOT_ZONE_ID,
    ) -> RouteResult | PipeRouteResult | StreamRouteResult | ExternalRouteResult:
        """Route virtual path to backend with mount-level access control."""
        virtual_path = self.validate_path(virtual_path)

        # DT_PIPE / DT_STREAM: kernel-native IPC dispatch at exact target
        # path. IPC inodes are endpoints (not prefixes), checked before
        # mount LPM. This is the only metastore.get() in route().
        meta = self._metastore.get(virtual_path)
        if meta is not None:
            if meta.is_pipe:
                return PipeRouteResult(path=virtual_path, metastore=self._metastore)
            if meta.is_stream:
                return StreamRouteResult(path=virtual_path, metastore=self._metastore)

        if self._kernel is not None:
            try:
                rust_result = self._kernel.route(virtual_path, zone_id, is_admin, check_write)
            except PermissionError as e:
                msg = str(e).replace(f"/{zone_id}/", "/").replace(f"/{zone_id}'", "/'")
                raise AccessDeniedError(msg) from None
            except ValueError:
                raise PathNotMountedError(virtual_path) from None

            info = self._dlc.get_mount_info_canonical(rust_result.mount_point)
            if info is None:
                raise PathNotMountedError(virtual_path)
            user_mp = extract_zone_id(rust_result.mount_point)[1]
            _route_meta = meta if meta is not None else self._metastore.get(user_mp)
            if _route_meta is not None and _route_meta.is_external_storage:
                return ExternalRouteResult(
                    backend=info.backend,
                    metastore=self._metastore,
                    backend_path=rust_result.backend_path,
                    mount_point=user_mp,
                    readonly=rust_result.readonly,
                    io_profile=rust_result.io_profile,
                )
            return RouteResult(
                backend=info.backend,
                metastore=self._metastore,
                backend_path=rust_result.backend_path,
                mount_point=user_mp,
                readonly=rust_result.readonly,
                io_profile=rust_result.io_profile,
            )

        # Python fallback: walk DLC mounts for LPM.
        result = self._lookup_lpm(virtual_path, zone_id)
        if result is None:
            raise PathNotMountedError(virtual_path)
        canonical_key, info = result
        canonical = canonicalize_path(virtual_path, zone_id)
        user_mp = extract_zone_id(canonical_key)[1]
        if info.admin_only and not is_admin:
            raise AccessDeniedError(f"Mount '{user_mp}' requires admin privileges")
        if info.readonly and check_write:
            raise AccessDeniedError(f"Mount '{user_mp}' is read-only")

        backend_path = self._strip_mount_prefix(canonical, canonical_key)

        _route_meta = meta if meta is not None else self._metastore.get(user_mp)
        if _route_meta is not None and _route_meta.is_external_storage:
            return ExternalRouteResult(
                backend=info.backend,
                metastore=self._metastore,
                backend_path=backend_path,
                mount_point=user_mp,
                readonly=info.readonly,
                io_profile=info.io_profile,
            )
        return RouteResult(
            backend=info.backend,
            metastore=self._metastore,
            backend_path=backend_path,
            mount_point=user_mp,
            readonly=info.readonly,
            io_profile=info.io_profile,
        )

    # ------------------------------------------------------------------
    # Mount table queries (delegate to DLC)
    # ------------------------------------------------------------------

    def get_mount_points(self) -> list[str]:
        """Return all active mount point paths (user-facing, no zone prefix)."""
        return self._dlc.mount_points()

    def has_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Check if an active mount exists at the given mount point."""
        return self._dlc.get_mount_info(mount_point, zone_id) is not None

    def get_mount(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> "MountInfo | None":
        """Get mount info for a specific mount point, or None if not found."""
        from nexus.core.path_utils import normalize_path
        from nexus.core.protocols.vfs_router import MountInfo

        info = self._dlc.get_mount_info(mount_point, zone_id)
        if info is None:
            return None
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return None
        return MountInfo(
            mount_point=normalized,
            readonly=info.readonly,
            admin_only=info.admin_only,
            backend=info.backend,
        )

    def get_mount_entry_for_path(
        self, path: str, zone_id: str = ROOT_ZONE_ID
    ) -> "_PyMountInfo | None":
        """Find the mount info covering *path* via longest-prefix match.

        Returns the ``_PyMountInfo`` (includes stream_backend_factory).
        For public mount info, use ``get_mount()`` instead.
        """
        result = self._lookup_lpm(path, zone_id)
        return result[1] if result is not None else None

    def list_mounts(self) -> "list[MountInfo]":
        """List all active mounts."""
        from nexus.core.protocols.vfs_router import MountInfo

        return sorted(
            [
                MountInfo(
                    mount_point=extract_zone_id(canonical_key)[1],
                    readonly=info.readonly,
                    admin_only=info.admin_only,
                    backend=info.backend,
                )
                for canonical_key, info in self._dlc.list_mounts()
            ],
            key=lambda m: m.mount_point,
        )

    def get_backend_by_name(self, name: str) -> "ObjectStoreABC | None":
        """Look up backend by name."""
        for _, info in self._dlc.list_mounts():
            if info.backend.name == name:
                return info.backend
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lookup_lpm(self, path: str, zone_id: str) -> "tuple[str, _PyMountInfo] | None":
        """Python-side longest-prefix match over the DLC mount map."""
        current = canonicalize_path(path, zone_id)
        entries = dict(self._dlc.list_mounts())
        while True:
            info = entries.get(current)
            if info is not None:
                return current, info
            if current == "/":
                return None
            current = posixpath.dirname(current)

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

        normalized = self._normalize_path(path)

        if not normalized.startswith("/"):
            raise InvalidPathError(path, "Path traversal detected")

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
        """Normalize virtual path."""
        if not path.startswith("/"):
            raise ValueError(f"Path must be absolute: {path}")

        normalized = posixpath.normpath(path)

        if not normalized.startswith("/"):
            raise ValueError(f"Path traversal detected: {path}")

        return normalized

    def _strip_mount_prefix(self, virtual_path: str, mount_point: str) -> str:
        """Strip mount prefix to get backend-relative path."""
        if virtual_path == mount_point:
            return ""
        if mount_point == "/":
            return virtual_path.lstrip("/")
        return virtual_path[len(mount_point) :].lstrip("/")
