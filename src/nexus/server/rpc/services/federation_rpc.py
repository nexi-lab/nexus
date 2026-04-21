"""FederationRPCService — gRPC RPC surface for federation / zone ops.

R20.18.5 Phase B: the pre-R20.18.5 version was a Python wrapper around
the now-deleted ``ZoneManager`` shim. This rebuild delegates every
``federation_*`` RPC to the kernel's ``zone_*`` methods (tolerated
tech debt per v20.10 plan — the boundary-rule-compliant replacement
is the ``/__sys__/zones/`` procfs view reached through ``sys_stat``
/ ``sys_readdir``; tracked as a post-merge follow-up).

Registered in ``fastapi_server.create_app`` when federation is active
(kernel reports ``mount_reconciliation_done()`` once bootstrap finishes).
"""

from __future__ import annotations

import contextlib
from typing import Any

from nexus.contracts.rpc import rpc_expose


class FederationRPCService:
    """Federation CRUD RPCs backed by ``nexus_kernel`` zone_* methods."""

    def __init__(self, kernel: Any, nexus_fs: Any = None) -> None:
        self._kernel = kernel
        self._nexus_fs = nexus_fs

    def _register_mount_in_python_dlc(self, mount_path: str, parent_zone: str) -> None:
        """Mirror Rust-side federation mount into the Python DriverLifecycle-
        Coordinator so ``PathRouter.route`` (which checks both Rust
        ``kernel.route()`` and ``_dlc.get_mount_info_canonical()``) finds
        the mount. Pre-R20.18.5 this happened via
        ``ZoneManager._on_mount_event -> coordinator._store_mount_info``.
        Kept here (tech debt) until the router consults the kernel
        directly for federation mounts.
        """
        nx = self._nexus_fs
        if nx is None:
            return
        coord = getattr(nx, "_driver_coordinator", None)
        if coord is None:
            return
        try:
            # Federation mounts inherit the root backend on this node;
            # look it up via the router and pass it through so the
            # _PyMountInfo has a non-None backend.
            root_backend = None
            with contextlib.suppress(Exception):
                root_backend = nx.router.route("/", zone_id=parent_zone).backend
            if root_backend is None:
                return
            coord._store_mount_info(mount_path, root_backend, zone_id=parent_zone)
        except Exception:
            # Silently absorb — failure here means Python-side routing
            # misses the mount, but Rust still has it; next call that
            # refreshes the DLC will fix.
            pass

    # ── Zone lifecycle ─────────────────────────────────────────────

    @rpc_expose(admin_only=True)
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        created = self._kernel.zone_create(zone_id)
        return {"zone_id": created}

    @rpc_expose(admin_only=True)
    def federation_remove_zone(self, zone_id: str, force: bool = False) -> dict[str, Any]:
        del force  # cascade unmount happens inside kernel apply-cb
        self._kernel.zone_remove(zone_id)
        return {"zone_id": zone_id, "removed": True}

    @rpc_expose(admin_only=True)
    def federation_join(self, zone_id: str) -> dict[str, Any]:
        joined = self._kernel.zone_join(zone_id)
        return {"zone_id": joined}

    # ── Mount topology ─────────────────────────────────────────────

    def _to_zone_relative(self, parent_zone: str, path: str) -> str:
        """Translate a VFS-global path to the parent zone's namespace.

        For parent=root the zone key equals the global path.  For nested
        mounts (parent=corp, path=/corp/sales) corp's namespace is
        zone-relative (/sales); the Rust raft ``ZoneManager::mount``
        writes the DT_MOUNT entry at the key we pass, so we must strip
        the parent's own mount prefix first. Pre-R20.18.5 this lived in
        ``ZoneManager._resolve_mount_parent`` (Python).
        """
        if parent_zone == "root" or not path.startswith("/"):
            return path
        # Walk the kernel's zone_list to find where parent_zone is
        # globally mounted, then strip that prefix.
        try:
            zones = list(self._kernel.zone_list())
        except Exception:
            return path
        # Build a "target_zone -> global_path" map by scanning the Rust
        # mount table via get_mount_points (returns /zone/mount_point
        # canonical keys).  We don't have direct access, but
        # has_mount("/corp", "root") is the heuristic signal.  Easiest:
        # if path starts with "/<parent_zone>/" heuristically, strip it.
        del zones
        candidate_prefix = f"/{parent_zone}"
        if path == candidate_prefix:
            return "/"
        if path.startswith(candidate_prefix + "/"):
            return path[len(candidate_prefix) :]
        # Fallback: pass through — matches root semantics.
        return path

    @rpc_expose(admin_only=True)
    def federation_mount(
        self,
        parent_zone: str,
        path: str,
        target_zone: str,
    ) -> dict[str, Any]:
        """Mount ``target_zone`` at ``path`` (global VFS) inside
        ``parent_zone``. Uses the legacy kwarg shape (parent_zone /
        path / target_zone) expected by the docker E2E test suite +
        CLI callers.
        """
        zone_relative = self._to_zone_relative(parent_zone, path)
        self._kernel.zone_mount(parent_zone, zone_relative, target_zone)
        # Mirror into Python DLC at the VFS-global path so
        # PathRouter.route (which receives global paths from callers)
        # can return non-None.
        self._register_mount_in_python_dlc(path, parent_zone)
        return {
            "parent_zone": parent_zone,
            "path": path,
            "target_zone": target_zone,
        }

    @rpc_expose(admin_only=True)
    def federation_unmount(
        self,
        parent_zone: str,
        path: str,
    ) -> dict[str, Any]:
        zone_relative = self._to_zone_relative(parent_zone, path)
        target = self._kernel.zone_unmount(parent_zone, zone_relative)
        # Mirror into Python DLC removal: without this the router's
        # _dlc cache still returns the old _PyMountInfo and the
        # unmounted path stays reachable via the mount-registered
        # path. Matches the pre-R20.18.5 unmount bookkeeping.
        nx = self._nexus_fs
        if nx is not None:
            coord = getattr(nx, "_driver_coordinator", None)
            if coord is not None:
                with contextlib.suppress(Exception):
                    coord.unmount(path, zone_id=parent_zone)
        return {"parent_zone": parent_zone, "path": path, "target_zone": target}

    @rpc_expose(admin_only=True)
    def federation_share(
        self,
        parent_zone_id: str,
        prefix: str,
        new_zone_id: str,
    ) -> dict[str, Any]:
        copied = self._kernel.zone_share(parent_zone_id, prefix, new_zone_id)
        return {
            "parent_zone_id": parent_zone_id,
            "prefix": prefix,
            "new_zone_id": new_zone_id,
            "entries_copied": copied,
        }

    # ── Introspection ──────────────────────────────────────────────

    def _links_count(self, zone_id: str) -> int:
        fn = getattr(self._kernel, "zone_links_count", None)
        if fn is None:
            return 0
        try:
            return int(fn(zone_id))
        except Exception:
            return 0

    @rpc_expose(admin_only=False)
    def federation_list_zones(self) -> dict[str, Any]:
        zone_ids: list[str] = list(self._kernel.zone_list())
        zones = [{"zone_id": zid, "links_count": self._links_count(zid)} for zid in zone_ids]
        return {"zones": zones, "node_id": zone_ids}

    @rpc_expose(admin_only=False)
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        # PyKernel.zone_cluster_info returns a Python dict already.
        status: dict[str, Any] = dict(self._kernel.zone_cluster_info(zone_id))
        status["links_count"] = self._links_count(zone_id)
        return status
