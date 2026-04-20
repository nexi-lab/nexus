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

from typing import Any

from nexus.contracts.rpc import rpc_expose


class FederationRPCService:
    """Federation CRUD RPCs backed by ``nexus_kernel`` zone_* methods."""

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel

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
        CLI callers. ``path`` may be either a zone-relative or global
        VFS path; the kernel auto-creates zones on first mount via
        R20.18.3's `resolve_federation_mount_backing`.
        """
        self._kernel.zone_mount(parent_zone, path, target_zone)
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
        target = self._kernel.zone_unmount(parent_zone, path)
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

    @rpc_expose(admin_only=False)
    def federation_list_zones(self) -> dict[str, Any]:
        zone_ids: list[str] = list(self._kernel.zone_list())
        zones = [{"zone_id": zid, "links_count": 0} for zid in zone_ids]
        return {"zones": zones, "node_id": zone_ids}

    @rpc_expose(admin_only=False)
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        # PyKernel.zone_cluster_info returns a Python dict already.
        status: dict[str, Any] = dict(self._kernel.zone_cluster_info(zone_id))
        # Callers also expect `links_count` — retained as 0 until a
        # dedicated kernel accessor lands (post-merge cleanup).
        status.setdefault("links_count", 0)
        return status
