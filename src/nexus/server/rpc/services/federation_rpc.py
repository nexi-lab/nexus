"""Federation RPC Service — explicit zone primitives + implicit mount.

Explicit (zone-aware):
    create_zone(path, zone_id)  — create zone + bind path (first node)
    join_zone(path, zone_id, peer?)  — join zone + bind path / cross-link
    unmount(path)  — unmount + cleanup

Implicit (mount, NFS-style):
    mount(source, target)  — auto-detect: cross-link / pull join / push create+join

Query:
    list_zones()  — list all zones
    cluster_info(zone_id)  — zone details
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class FederationRPCService:
    """RPC surface for federation: explicit zone ops + implicit mount."""

    def __init__(self, zone_manager: Any, federation: Any | None = None) -> None:
        self._zone_manager = zone_manager
        self._federation = federation

    # ── Query ────────────────────────────────────────────────────────

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

    # ── Explicit: zone primitives ────────────────────────────────────

    @rpc_expose(admin_only=True, description="Create a zone and bind it to a path")
    def federation_create_zone(self, path: str, zone_id: str) -> dict[str, Any]:
        """Create a new zone from a subtree at *path*.

        This is the first step in federation: carve out a path into its
        own Raft zone so other nodes can join it later.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        parent_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID
        new_zone_id = self._zone_manager.share_subtree(
            parent_zone_id=parent_zone,
            path=path,
            zone_id=zone_id,
        )
        logger.info("Zone '%s' created at '%s'", new_zone_id, path)
        return {"zone_id": new_zone_id, "path": path, "created": True}

    @rpc_expose(admin_only=True, description="Join a zone and bind it to a local path")
    async def federation_join_zone(
        self,
        path: str,
        zone_id: str,
        peer: str | None = None,
    ) -> dict[str, Any]:
        """Join an existing zone at *path*.

        Three modes:
        - peer given: join remote zone via Raft ConfChange (pull)
        - peer=None, zone exists locally: cross-link (bind mount)
        - peer=None, zone not local: error
        """
        store = self._zone_manager.get_store(zone_id)
        from nexus.contracts.constants import ROOT_ZONE_ID

        root_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID

        if peer is not None:
            # Remote join: create local Raft replica + request membership
            self._zone_manager.join_zone(zone_id, peers=[peer])
            if self._federation is not None:
                await self._federation._request_membership(
                    peer_addr=peer,
                    zone_id=zone_id,
                    node_id=self._zone_manager.node_id,
                    node_address=getattr(self._zone_manager, "advertise_addr", peer),
                )
            # Mount at path
            self._zone_manager.mount(root_zone, path, zone_id)
            logger.info("Joined zone '%s' from %s, mounted at '%s'", zone_id, peer, path)
            return {"zone_id": zone_id, "path": path, "peer": peer, "joined": True}

        if store is not None:
            # Cross-link: zone already exists locally, just add mount point
            self._zone_manager.mount(root_zone, path, zone_id)
            logger.info("Cross-linked zone '%s' at '%s'", zone_id, path)
            return {"zone_id": zone_id, "path": path, "cross_link": True}

        raise RuntimeError(
            f"Zone '{zone_id}' not found locally and no peer specified. "
            f"Use peer= to join a remote zone."
        )

    # ── Implicit: NFS-style mount ────────────────────────────────────

    @rpc_expose(
        admin_only=True, description="Mount (NFS-style): auto-detect cross-link / pull / push"
    )
    async def federation_mount(
        self,
        source: str,
        target: str,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """NFS-style mount with auto-detection.

        Formats:
            mount /src /dst          — cross-link (both local)
            mount host:/path /local  — pull join (remote → local)
            mount /local host:/path  — push create+join (local → remote)

        Args:
            source: Source path. Prefix with "host:" for remote.
            target: Target path. Prefix with "host:" for remote.
            zone_id: Explicit zone ID (auto-generated if not given).
        """
        src_remote, src_host, src_path = _parse_mount_arg(source)
        tgt_remote, tgt_host, tgt_path = _parse_mount_arg(target)

        if not src_remote and not tgt_remote:
            # Both local → cross-link
            return await self._mount_crosslink(src_path, tgt_path)

        if src_remote and not tgt_remote:
            # Remote source → local target: pull join
            return await self._mount_pull(src_host, src_path, tgt_path)

        if not src_remote and tgt_remote:
            # Local source → remote target: push create + remote join
            return await self._mount_push(src_path, tgt_host, tgt_path, zone_id)

        raise ValueError("Both source and target cannot be remote")

    # ── Unmount ──────────────────────────────────────────────────────

    @rpc_expose(admin_only=True, description="Unmount a zone from a path")
    def federation_unmount(self, path: str) -> dict[str, Any]:
        """Unmount zone at path.

        Resolves the parent zone from the path automatically.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        parent_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID
        self._zone_manager.unmount(parent_zone, path)
        logger.info("Unmounted '%s'", path)
        return {"path": path, "unmounted": True}

    # ── Private helpers ──────────────────────────────────────────────

    async def _mount_crosslink(self, src_path: str, tgt_path: str) -> dict[str, Any]:
        """Both paths local — find zone at src, mount at tgt."""
        from nexus.contracts.constants import ROOT_ZONE_ID

        root_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID
        root_store = self._zone_manager.get_store(root_zone)
        if root_store is None:
            raise RuntimeError("Root zone not found")

        meta = root_store.get(src_path)
        if meta is None or not meta.is_mount:
            raise ValueError(f"'{src_path}' is not a mount point — cannot cross-link")

        zone_id = meta.target_zone_id
        if not zone_id:
            raise ValueError(f"'{src_path}' has no target zone")

        self._zone_manager.mount(root_zone, tgt_path, zone_id)
        logger.info("Cross-linked '%s' → '%s' (zone '%s')", src_path, tgt_path, zone_id)
        return {"source": src_path, "target": tgt_path, "zone_id": zone_id, "cross_link": True}

    async def _mount_pull(
        self, remote_host: str, remote_path: str, local_path: str
    ) -> dict[str, Any]:
        """Remote → local: discover zone on remote, join it locally."""
        if self._federation is None:
            raise RuntimeError("Federation not configured")

        result_zone_id: str = await self._federation.join(
            peer_addr=remote_host,
            remote_path=remote_path,
            local_path=local_path,
        )
        logger.info(
            "Mounted %s:%s → %s (zone '%s')", remote_host, remote_path, local_path, result_zone_id
        )
        return {
            "source": f"{remote_host}:{remote_path}",
            "target": local_path,
            "zone_id": result_zone_id,
            "mounted": True,
        }

    async def _mount_push(
        self, local_path: str, remote_host: str, remote_path: str, zone_id: str | None
    ) -> dict[str, Any]:
        """Local → remote: create zone locally, push join to remote node."""
        if self._federation is None:
            raise RuntimeError("Federation not configured")

        import uuid

        from nexus.contracts.constants import ROOT_ZONE_ID

        parent_zone = self._zone_manager.root_zone_id or ROOT_ZONE_ID
        new_zone_id = zone_id or str(uuid.uuid4())

        # Step 1: Create zone locally
        self._zone_manager.share_subtree(
            parent_zone_id=parent_zone,
            path=local_path,
            zone_id=new_zone_id,
        )

        # Step 2: Push join to remote node via gRPC
        # TODO: Implement federation.push_join() — requires new gRPC RPC
        # for remote node to join_zone + mount. For now, remote node must
        # explicitly call federation_join_zone.
        raise NotImplementedError(
            f"Push mount (local→remote) not yet implemented. "
            f"Use federation_join_zone on {remote_host} to join zone '{new_zone_id}' manually."
        )

        logger.info(
            "Mounted %s → %s:%s (zone '%s')", local_path, remote_host, remote_path, new_zone_id
        )
        return {
            "source": local_path,
            "target": f"{remote_host}:{remote_path}",
            "zone_id": new_zone_id,
            "mounted": True,
        }


def _parse_mount_arg(arg: str) -> tuple[bool, str, str]:
    """Parse 'host:/path' or '/path' → (is_remote, host, path)."""
    if ":" in arg and not arg.startswith("/"):
        host, path = arg.split(":", 1)
        return True, host, path
    return False, "", arg
