"""MountTable — kernel mount table, like Linux mount_hashtable.

Independent kernel data structure. Not owned by any subsystem:
- DriverLifecycleCoordinator writes it (mount/unmount lifecycle)
- PathRouter reads it (LPM path routing)
- NexusFS reads it (list_mounts, get_mount_points)

Linux analogy: ``mount_hashtable`` + per-namespace ``mnt_namespace``.
Accessible via ``/__sys__/mounts`` (like ``/proc/mounts``).

Issue #3584.
"""

from __future__ import annotations

import contextlib
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# RUST_FALLBACK: canonicalize_path, extract_zone_id, RustPathRouter
from nexus._rust_compat import (
    RustPathRouter,
)
from nexus._rust_compat import (
    canonicalize_path as _rust_canonicalize_path,
)
from nexus._rust_compat import (
    extract_zone_id as _rust_extract_zone_id,
)
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.path_utils import normalize_path

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC
    from nexus.core.object_store import ObjectStoreABC


# ---------------------------------------------------------------------------
# Zone-canonical path helpers (pure functions, ~0 cost)
# ---------------------------------------------------------------------------

_RUST_ZONE_AVAILABLE = _rust_canonicalize_path is not None


def canonicalize_path(path: str, zone_id: str = ROOT_ZONE_ID) -> str:
    """Canonicalize a virtual path with zone prefix for routing.

    ``canonicalize_path("/workspace/file.txt", "root")``
    → ``"/root/workspace/file.txt"``
    """
    # RUST_FALLBACK: canonicalize_path
    if _RUST_ZONE_AVAILABLE:
        return str(_rust_canonicalize_path(path, zone_id))
    stripped = path.lstrip("/")
    return f"/{zone_id}/{stripped}" if stripped else f"/{zone_id}"


def extract_zone_id(canonical_path: str) -> tuple[str, str]:
    """Extract (zone_id, relative_path) from a canonical path.

    ``extract_zone_id("/root/workspace/file.txt")``
    → ``("root", "/workspace/file.txt")``
    """
    # RUST_FALLBACK: extract_zone_id
    if _RUST_ZONE_AVAILABLE:
        result = _rust_extract_zone_id(canonical_path)
        return (str(result[0]), str(result[1]))
    parts = canonical_path.lstrip("/").split("/", 1)
    zone_id = parts[0]
    relative = "/" + parts[1] if len(parts) > 1 else "/"
    return zone_id, relative


# ---------------------------------------------------------------------------
# Mount entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MountEntry:
    """Runtime mount entry — holds Python objects that cannot be serialized.

    The ``backend`` field is typed ``ObjectStoreABC`` — the kernel's file
    operations contract.  ``metastore`` is the zone's MetastoreABC instance
    (per-zone Raft store).  Like Linux ``struct super_block *`` in the mount
    table.
    """

    backend: "ObjectStoreABC"
    metastore: "MetastoreABC"
    readonly: bool
    admin_only: bool
    io_profile: str
    stream_backend_factory: Any = None
    is_external: bool = False


# ---------------------------------------------------------------------------
# Mount table
# ---------------------------------------------------------------------------


class MountTable:
    """Kernel mount table — independent data, like Linux mount_hashtable.

    Written by DriverLifecycleCoordinator.mount()/unmount().
    Read by PathRouter.route() and kernel list/get operations.

    Zone-canonical keys: ``/{zone_id}/{mount_point}`` so LPM naturally
    distinguishes zones.
    """

    __slots__ = ("_entries", "_rust", "_default_metastore", "_kernel")

    def __init__(self, default_metastore: "MetastoreABC") -> None:
        self._entries: dict[str, MountEntry] = {}
        self._default_metastore: MetastoreABC = default_metastore
        # RUST_FALLBACK: RustPathRouter — LPM acceleration
        self._rust: Any = RustPathRouter() if RustPathRouter is not None else None
        self._kernel: Any = None  # late-bound; set by NexusFS after Kernel is created

    def bind_kernel(self, kernel: Any) -> None:
        """Late-bind the Rust kernel and backfill any pre-existing mounts.

        Tests (and any caller that adds mounts before NexusFS construction)
        can register entries on a bare MountTable. NexusFS calls this method
        after the kernel exists so the kernel router sees the same mounts as
        Python.  Without backfill, sys_mkdir/sys_write fail with
        ``No mount found for path: <...>`` even though the mount exists in
        ``self._entries``.
        """
        self._kernel = kernel
        if not self._entries:
            return
        for canonical, entry in self._entries.items():
            # canonical key is "/{zone_id}{mount_point}"; recover them.
            if canonical.startswith("/"):
                first_slash = canonical.find("/", 1)
                if first_slash > 0:
                    zone_id = canonical[1:first_slash]
                    mount_point = canonical[first_slash:] or "/"
                else:
                    zone_id = canonical[1:] or ROOT_ZONE_ID
                    mount_point = "/"
            else:
                zone_id = ROOT_ZONE_ID
                mount_point = canonical
            backend = entry.backend
            _backend_name = str(getattr(backend, "name", "unknown"))
            _is_cas_local = getattr(backend, "has_root_path", False) and type(
                backend
            ).__name__.startswith("CAS")
            _local_root = str(getattr(backend, "root_path", None)) if _is_cas_local else None
            _ms = entry.metastore
            _ms_path = getattr(_ms, "_redb_path", None) if _ms else None
            _bk_kwargs: dict = {
                "py_backend": backend,
                "metastore_path": str(_ms_path) if _ms_path else None,
            }
            # is_external added in Task 2 (Issue #3710); guard for older kernels.
            try:
                kernel.add_mount(
                    mount_point,
                    zone_id,
                    entry.readonly,
                    entry.admin_only,
                    entry.io_profile,
                    _backend_name,
                    _local_root,
                    True,
                    is_external=entry.is_external,
                    **_bk_kwargs,
                )
            except TypeError:
                with contextlib.suppress(Exception):
                    kernel.add_mount(
                        mount_point,
                        zone_id,
                        entry.readonly,
                        entry.admin_only,
                        entry.io_profile,
                        _backend_name,
                        _local_root,
                        True,
                        **_bk_kwargs,
                    )

    # -- Write operations (called by coordinator) ---------------------------

    def add(
        self,
        mount_point: str,
        backend: "ObjectStoreABC",
        *,
        metastore: "MetastoreABC | None" = None,
        readonly: bool = False,
        admin_only: bool = False,
        io_profile: str = "balanced",
        stream_backend_factory: Any = None,
        zone_id: str = ROOT_ZONE_ID,
        is_external: bool = False,
    ) -> None:
        """Add a mount entry. Called by coordinator.mount()."""
        mount_point = normalize_path(mount_point)
        canonical = canonicalize_path(mount_point, zone_id)
        self._entries[canonical] = MountEntry(
            backend=backend,
            metastore=metastore or self._default_metastore,
            readonly=readonly,
            admin_only=admin_only,
            io_profile=io_profile,
            stream_backend_factory=stream_backend_factory,
            is_external=is_external,
        )
        _backend_name = backend.name
        if not isinstance(_backend_name, str):
            _backend_name = str(_backend_name)
        # Only CAS backends with local roots get Rust CasLocalBackend (pure Rust,
        # zero GIL).  All other backends → PyObjectStoreAdapter (GIL cold path).
        # Detection: CASLocalBackend has has_root_path=True AND uses CAS addressing
        # (has _cas_engine or class name starts with "CAS").
        _is_cas_local = getattr(backend, "has_root_path", False) and type(
            backend
        ).__name__.startswith("CAS")
        _local_root = str(getattr(backend, "root_path", None)) if _is_cas_local else None
        if self._rust is not None:
            self._rust.add_mount(
                mount_point,
                zone_id,
                readonly,
                admin_only,
                io_profile,
                _backend_name,
                _local_root,
                True,  # fsync
            )
        # Also wire into Kernel router (Issue #1868) so sys_read/sys_stat
        # fast path sees live mounts.  Pass py_backend= when no local_root
        # so Rust wraps Python backend via PyObjectStoreAdapter.
        # Atomic: if kernel add_mount fails, roll back Python _entries to
        # avoid split-brain state where Python routing sees the mount but
        # kernel does not.
        if self._kernel is not None:
            _ms = self._entries[canonical].metastore
            _ms_path = getattr(_ms, "_redb_path", None) if _ms else None
            _kwargs: dict = {
                "py_backend": backend,
                "metastore_path": str(_ms_path) if _ms_path else None,
            }
            # is_external added in Task 2 (Issue #3710); guard for older kernels.
            try:
                try:
                    self._kernel.add_mount(
                        mount_point,
                        zone_id,
                        readonly,
                        admin_only,
                        io_profile,
                        _backend_name,
                        _local_root,
                        True,
                        is_external=is_external,
                        **_kwargs,
                    )
                except TypeError:
                    # Kernel binary predates is_external — fall back without it.
                    self._kernel.add_mount(
                        mount_point,
                        zone_id,
                        readonly,
                        admin_only,
                        io_profile,
                        _backend_name,
                        _local_root,
                        True,
                        **_kwargs,
                    )
            except Exception:
                # Roll back Python-side registration on kernel failure.
                self._entries.pop(canonical, None)
                if self._rust is not None:
                    with contextlib.suppress(Exception):
                        self._rust.remove_mount(mount_point, zone_id)
                raise

    def remove(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Remove a mount entry. Called by coordinator.unmount().

        Returns True if removed, False if not found.
        """
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return False
        canonical = canonicalize_path(normalized, zone_id)
        if canonical not in self._entries:
            return False
        del self._entries[canonical]
        if self._rust is not None:
            self._rust.remove_mount(normalized, zone_id)
        if self._kernel is not None:
            with contextlib.suppress(Exception):
                self._kernel.remove_mount(normalized, zone_id)
        return True

    # -- Read operations (called by router, kernel) -------------------------

    def get(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> MountEntry | None:
        """Exact match lookup."""
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return None
        canonical = canonicalize_path(normalized, zone_id)
        return self._entries.get(canonical)

    def get_canonical(self, canonical_key: str) -> MountEntry | None:
        """Direct lookup by canonical key (used by router after Rust LPM)."""
        return self._entries.get(canonical_key)

    def has(self, mount_point: str, zone_id: str = ROOT_ZONE_ID) -> bool:
        """Check if mount exists."""
        try:
            normalized = normalize_path(mount_point)
        except ValueError:
            return False
        return canonicalize_path(normalized, zone_id) in self._entries

    def lookup_lpm(self, path: str, zone_id: str = ROOT_ZONE_ID) -> tuple[str, MountEntry] | None:
        """Longest prefix match (Python fallback when Rust unavailable).

        Returns (canonical_key, entry) or None.
        """
        current = canonicalize_path(path, zone_id)
        while True:
            entry = self._entries.get(current)
            if entry is not None:
                return current, entry
            if current == "/":
                return None
            current = posixpath.dirname(current)

    def mount_points(self) -> list[str]:
        """All user-facing mount point paths (no zone prefix)."""
        return sorted(extract_zone_id(key)[1] for key in self._entries)

    def items(self) -> list[tuple[str, MountEntry]]:
        """All (canonical_key, entry) pairs."""
        return list(self._entries.items())

    @property
    def rust(self) -> Any:
        """Rust LPM engine (if available). Used by PathRouter for fast routing."""
        return self._rust
