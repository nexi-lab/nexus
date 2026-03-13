"""Path routing for mapping virtual paths to storage backends.

PathRouter = Linux VFS mount table. Routes virtual paths to storage backends
using longest-prefix matching with mount-level access control (readonly,
admin_only). No namespace or zone concepts — those belong in ReBAC and
federation layers respectively.

Mount table is persisted in MetastoreABC as DT_MOUNT entries (source of truth).
The only in-memory state is ``_backends`` — a registry of runtime backend
instances that cannot be serialized to metastore.

route() performs LPM by walking path components from deepest to shallowest,
checking metastore for DT_MOUNT at each level. Metastore's Rust-level
in-memory cache (redb) provides ~5 μs reads — no Python cache needed.

Architecture:
    global_path → ZonePathResolver → (zone_id, local_path) → PathRouter.route() → (backend, backend_path)
"""

import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.contracts.exceptions import AccessDeniedError, InvalidPathError, PathNotMountedError
from nexus.contracts.metadata import DT_MOUNT, FileMetadata

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC
    from nexus.core.protocols.vfs_router import MountInfo


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


class PathRouter:
    """Route virtual paths to storage backends using mount table.

    Design Principles:
    1. **Longest Prefix Match**: most specific mount wins (deepest path).
    2. **Mount-level access control**: ``readonly`` and ``admin_only`` as mount options.
    3. **Metastore-backed**: DT_MOUNT entries in MetastoreABC are the source of truth.
       Only backend instances live in-memory (``_backends`` registry).

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

    def add_mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None:
        """Add a mount to the router.

        Writes a DT_MOUNT entry to metastore and registers the backend.
        If a mount already exists at the same path, it is replaced.

        Args:
            mount_point: Virtual path prefix (must start with /).
            backend: ObjectStoreABC instance (kernel file_operations contract).
            readonly: Whether mount is readonly.
            admin_only: Whether mount requires admin privileges.
            io_profile: I/O tuning profile.

        Raises:
            ValueError: If mount_point is invalid.
        """
        mount_point = self._normalize_path(mount_point)

        # Persist DT_MOUNT entry in metastore (source of truth)
        meta = FileMetadata(
            path=mount_point,
            backend_name=backend.name,
            physical_path=mount_point,
            size=0,
            entry_type=DT_MOUNT,
        )
        self._metastore.put(meta)
        self._register_mount_entry(
            mount_point,
            backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

    def add_runtime_mount(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
    ) -> None:
        """Register an in-memory mount without persisting metadata.

        Used for ephemeral runtime mounts where the metastore is not the
        source of truth, such as the REMOTE client-side root mount.
        """
        mount_point = self._normalize_path(mount_point)
        self._register_mount_entry(
            mount_point,
            backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

    def _register_mount_entry(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        readonly: bool,
        admin_only: bool,
        io_profile: str,
    ) -> None:
        """Register the runtime mount entry for path routing."""
        self._backends[mount_point] = _MountEntry(
            backend=backend,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
        )

    def route(
        self,
        virtual_path: str,
        *,
        is_admin: bool = False,
        check_write: bool = False,
    ) -> RouteResult | PipeRouteResult | StreamRouteResult:
        """Route virtual path to backend with mount-level access control.

        Algorithm: walk path components from deepest to shallowest, checking
        metastore for DT_MOUNT at each level (longest prefix match).  Each
        metastore.get() is ~5 μs with redb's Rust in-memory cache.

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
            StreamRouteResult for DT_STREAM.

        Raises:
            PathNotMountedError: No mount found for path.
            AccessDeniedError: Access denied by mount-level rules.
            InvalidPathError: Path validation failed.
        """
        virtual_path = self.validate_path(virtual_path)

        # LPM: walk from deepest prefix to shallowest
        current = virtual_path
        while True:
            meta = self._metastore.get(current)

            # DT_PIPE / DT_STREAM: kernel-native IPC dispatch at exact
            # target path. IPC inodes are endpoints (not prefixes), so
            # only match on the first iteration (current == virtual_path).
            if meta is not None and current == virtual_path:
                if meta.is_pipe:
                    return PipeRouteResult(path=virtual_path)
                if meta.is_stream:
                    return StreamRouteResult(path=virtual_path)

            # Primary: metastore DT_MOUNT (persistent, cross-session).
            # Fallback: _backends registry (in-memory, current session).
            # The fallback is required for REMOTE profile where the
            # server "stat" RPC does not return entry_type, so
            # metastore.get() never reports is_mount=True.
            if (meta is not None and meta.is_mount) or current in self._backends:
                entry = self._backends.get(current)
                if entry is None:
                    # DT_MOUNT in metastore but backend not loaded
                    # (stale mount from previous session or remote zone)
                    raise PathNotMountedError(virtual_path)

                # Mount-level access control (like Linux mount options)
                if entry.admin_only and not is_admin:
                    raise AccessDeniedError(f"Mount '{current}' requires admin privileges")
                if entry.readonly and check_write:
                    raise AccessDeniedError(f"Mount '{current}' is read-only")

                backend_path = self._strip_mount_prefix(virtual_path, current)
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

    def has_mount(self, mount_point: str) -> bool:
        """Check if an active mount exists at the given mount point."""
        try:
            normalized = self._normalize_path(mount_point)
            return normalized in self._backends
        except ValueError:
            return False

    def get_mount(self, mount_point: str) -> "MountInfo | None":
        """Get mount info for a specific mount point, or None if not found."""
        from nexus.core.protocols.vfs_router import MountInfo

        try:
            normalized = self._normalize_path(mount_point)
            entry = self._backends.get(normalized)
            if entry is None:
                return None
            return MountInfo(
                mount_point=normalized,
                readonly=entry.readonly,
                admin_only=entry.admin_only,
                backend=entry.backend,
            )
        except ValueError:
            return None

    def remove_mount(self, mount_point: str) -> bool:
        """Remove a mount by its mount point.

        Deletes the DT_MOUNT entry from metastore and unregisters the backend.

        Returns:
            True if mount was removed, False if not found.
        """
        try:
            normalized = self._normalize_path(mount_point)
            if normalized in self._backends:
                del self._backends[normalized]
                self._metastore.delete(normalized)
                return True
            # Fallback: stale DT_MOUNT in metastore without a loaded backend
            meta = self._metastore.get(normalized)
            if meta is not None and meta.is_mount:
                self._metastore.delete(normalized)
                return True
            return False
        except ValueError:
            return False

    def list_mounts(self) -> "list[MountInfo]":
        """List all mounts, including stale DT_MOUNT entries without loaded backends."""
        from nexus.core.protocols.vfs_router import MountInfo

        active_mps: set[str] = set(self._backends.keys())
        result: list[MountInfo] = [
            MountInfo(
                mount_point=mp,
                readonly=entry.readonly,
                admin_only=entry.admin_only,
                backend=entry.backend,
            )
            for mp, entry in sorted(self._backends.items())
        ]

        # Stale: DT_MOUNT in metastore but no loaded backend
        for meta in self._metastore.list("/"):
            if meta.is_mount and meta.path not in active_mps:
                result.append(MountInfo(mount_point=meta.path, readonly=False, status="stale"))

        return sorted(result, key=lambda m: m.mount_point)

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

        if any(ord(c) < 32 for c in path if c not in ("\t", "\n")):
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
