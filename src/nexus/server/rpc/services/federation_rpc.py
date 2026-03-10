"""Federation RPC Service — zone lifecycle, share/join, mounts.

Issue #1520.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class FederationRPCService:
    """RPC surface for federation zone lifecycle, mounts, share, and join."""

    def __init__(self, zone_manager: Any, federation: Any | None = None) -> None:
        self._zone_manager = zone_manager
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
        return {
            "zone_id": zone_id,
            "node_id": self._zone_manager.node_id,
            "links_count": self._zone_manager.get_links_count(zone_id),
            "has_store": store is not None,
        }

    @rpc_expose(admin_only=True, description="Create a new Raft zone")
    def federation_create_zone(self, zone_id: str) -> dict[str, Any]:
        self._zone_manager.create_zone(zone_id)
        logger.info("Zone '%s' created via RPC", zone_id)
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
        zone_id: str = await self._federation.join(
            peer_addr=peer_addr, remote_path=remote_path, local_path=local_path
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
        self._zone_manager.mount(parent_zone, path, target_zone)
        logger.info(
            "Zone '%s' mounted at '%s' in zone '%s' via RPC",
            target_zone,
            path,
            parent_zone,
        )
        return {
            "parent_zone_id": parent_zone,
            "mount_path": path,
            "target_zone_id": target_zone,
            "mounted": True,
        }

    @rpc_expose(admin_only=True, description="Unmount a zone from a path")
    def federation_unmount(self, parent_zone: str, path: str) -> dict[str, Any]:
        self._zone_manager.unmount(parent_zone, path)
        logger.info("Unmounted '%s' from zone '%s' via RPC", path, parent_zone)
        return {
            "parent_zone_id": parent_zone,
            "mount_path": path,
            "unmounted": True,
        }
