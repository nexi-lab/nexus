"""Federation RPC Service — zone lifecycle, share/join, mounts.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class FederationRPCService:
    """RPC surface for federation zone lifecycle, mounts, share, and join."""

    def __init__(self, federation: Any) -> None:
        self._zone_manager = federation.zone_manager
        self._federation = federation

    @rpc_expose(admin_only=True, description="List all federation zones with link counts")
    def federation_list_zones(self) -> dict[str, Any]:
        zone_ids: list[str] = self._zone_manager.list_zones()
        zones = [
            {
                "zone_id": zid,
                "links_count": self._zone_manager.get_links_count(zid),
            }
            for zid in zone_ids
        ]
        return {"zones": zones}

    @rpc_expose(admin_only=True, description="Get cluster info for a zone")
    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        store = self._zone_manager.get_store(zone_id)
        is_leader = store.is_leader() if store is not None else False
        return {
            "zone_id": zone_id,
            "node_id": self._zone_manager.node_id,
            "links_count": self._zone_manager.get_links_count(zone_id),
            "has_store": store is not None,
            "is_leader": is_leader,
        }

    @rpc_expose(admin_only=True, description="Create a new Raft zone")
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        if self._federation is not None:
            # Federation.create_zone includes all cluster peers in the Raft group
            self._federation.create_zone(zone_id)
        else:
            # Fallback: single-node (no peers)
            self._zone_manager.create_zone(zone_id)
        logger.info("Zone '%s' created via RPC", zone_id)
        # R16.2: Rust ``PyZoneManager.create_zone`` triggers a catch-up
        # scan of historic DT_MOUNT entries after the zone is opened;
        # the apply-event hook picks up any mounts whose target is now
        # local. No manual reconcile_mounts_from_raft call needed.
        return {"zone_id": zone_id, "created": True}

    @rpc_expose(admin_only=True, description="Remove a Raft zone")
    def federation_remove_zone(self, zone_id: str) -> dict[str, Any]:
        self._zone_manager.remove_zone(zone_id)
        logger.info("Zone '%s' removed via RPC", zone_id)
        return {"zone_id": zone_id, "removed": True}

    @rpc_expose(description="Share a local subtree as a federation zone")
    async def federation_share(self, local_path: str, zone_id: str | None = None) -> dict[str, Any]:
        if self._federation is None:
            raise RuntimeError("Federation not configured")
        new_zone_id: str = await self._federation.share(local_path=local_path, zone_id=zone_id)
        logger.info("Shared '%s' as zone '%s' via RPC", local_path, new_zone_id)
        return {"zone_id": new_zone_id, "local_path": local_path, "shared": True}

    @rpc_expose(description="Join a peer's shared zone via federation protocol")
    async def federation_join(
        self, peer_addr: str, remote_path: str, local_path: str
    ) -> dict[str, Any]:
        if self._federation is None:
            raise RuntimeError("Federation not configured")
        from nexus.raft.peer_address import PeerAddress

        parsed = PeerAddress.parse(peer_addr)
        zone_id: str = await self._federation.join(
            peer_addr=parsed.grpc_target, remote_path=remote_path, local_path=local_path
        )
        logger.info(
            "Joined zone '%s' from %s via RPC, mounted at '%s'",
            zone_id,
            peer_addr,
            local_path,
        )
        return {
            "zone_id": zone_id,
            "peer_addr": peer_addr,
            "local_path": local_path,
            "joined": True,
        }

    @rpc_expose(admin_only=True, description="Mount a zone at a path in another zone")
    def federation_mount(self, parent_zone: str, path: str, target_zone: str) -> dict[str, Any]:
        # Resolve global path → zone-relative path for non-root zones.
        # User gives global path (e.g. "/corp/eng"), zone_manager needs
        # zone-relative path (e.g. "/eng" in zone "corp").
        zone_path = self._to_zone_relative(parent_zone, path)
        self._zone_manager.mount(parent_zone, zone_path, target_zone, global_path=path)
        logger.info(
            "Zone '%s' mounted at '%s' (zone-relative: '%s') in zone '%s' via RPC",
            target_zone,
            path,
            zone_path,
            parent_zone,
        )
        # R16.2: the DT_MOUNT proposed by the mount() shim above fires
        # a MountEvent on apply — both on this node and on every peer
        # that replicates it. The consumer task wires DLC without a
        # manual reconcile call here.
        return {
            "parent_zone_id": parent_zone,
            "mount_path": path,
            "target_zone_id": target_zone,
            "mounted": True,
        }

    @rpc_expose(admin_only=True, description="Unmount a zone from a path")
    def federation_unmount(self, parent_zone: str, path: str) -> dict[str, Any]:
        zone_path = self._to_zone_relative(parent_zone, path)
        self._zone_manager.unmount(parent_zone, zone_path, global_path=path)
        logger.info("Unmounted '%s' from zone '%s' via RPC", path, parent_zone)
        return {
            "parent_zone_id": parent_zone,
            "mount_path": path,
            "unmounted": True,
        }

    def _to_zone_relative(self, zone_id: str, global_path: str) -> str:
        """Convert global path to zone-relative path.

        For root zone, global and zone-relative are the same.
        For non-root zones, strip the mount prefix.

        Example: zone="corp" mounted at "/corp" → "/corp/eng" becomes "/eng"
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        root_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID
        if zone_id == root_zone:
            return global_path

        # Find where this zone is mounted by scanning root zone's metastore
        root_store = self._zone_manager.get_store(root_zone)
        if root_store is None:
            return global_path

        # Walk path prefixes to find mount point for this zone
        parts = global_path.strip("/").split("/")
        for i in range(len(parts)):
            prefix = "/" + "/".join(parts[: i + 1])
            meta = root_store.get(prefix)
            if meta is not None and meta.is_mount and meta.target_zone_id == zone_id:
                # Strip mount prefix
                relative = global_path[len(prefix) :]
                return relative if relative else "/"

        # No mount found — assume path is already zone-relative
        return global_path
