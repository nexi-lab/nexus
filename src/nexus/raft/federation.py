"""NexusFederation — optional DI subsystem for multi-node zone sharing.

Federation is NOT kernel. It is an optional subsystem at the same level as
CacheStore/RecordStore. Without federation, NexusFS gracefully degrades to
client-server mode (RemoteNexusFS) or single-node standalone mode.

Layering:
    NexusFederation (this file) — orchestration / service layer
    ├── ZoneManager             — HAL (wraps PyO3 Rust driver)
    │   └── PyZoneManager       — Driver (Rust/redb/Raft)
    └── RaftClient              — peer gRPC communication

No ABC needed: federation is inherently asymmetric (local ZoneManager +
remote RaftClient). No "remote federation proxy" scenario exists.

See: docs/architecture/federation-memo.md §6.9
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class NexusFederation:
    """Orchestrate zone sharing and joining across nodes.

    Combines ZoneManager (local zone ops) with RaftClient (peer gRPC)
    into high-level share/join workflows. Dependencies are injected.

    Usage:
        from nexus.raft.zone_manager import ZoneManager
        from nexus.raft.client import RaftClient

        mgr = ZoneManager(node_id=1, base_path="./zones", bind_addr="0.0.0.0:2126")
        mgr.bootstrap()

        fed = NexusFederation(
            zone_manager=mgr,
            client_factory=lambda addr: RaftClient(address=addr),
        )

        # Alice shares with Bob
        zone_id = await fed.share("/usr/alice/projectA", "bob:2126", "/shared-projectA")

        # Charlie joins via Bob
        zone_id = await fed.join("bob:2126", "/shared-projectA", "/usr/charlie/shared")
    """

    def __init__(
        self,
        zone_manager: Any,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        """Initialize federation orchestrator.

        Args:
            zone_manager: ZoneManager instance for local zone operations.
            client_factory: Factory that creates a RaftClient for a given
                peer address. If None, auto-creates RaftClients with TLS
                config from the ZoneManager.
        """
        self._mgr = zone_manager
        if client_factory is not None:
            self._client_factory = client_factory
        else:
            # Auto-build factory with TLS config from ZoneManager
            from nexus.raft.client import RaftClient, RaftClientConfig

            tls_cert = getattr(zone_manager, "_tls_cert_path", None)
            tls_key = getattr(zone_manager, "_tls_key_path", None)
            tls_ca = getattr(zone_manager, "_tls_ca_path", None)
            config = RaftClientConfig(
                tls_cert_path=tls_cert,
                tls_key_path=tls_key,
                tls_ca_path=tls_ca,
            )
            self._client_factory = lambda addr: RaftClient(address=addr, config=config)

    async def share(
        self,
        local_path: str,
        peer_addr: str,
        remote_path: str,
        zone_id: str | None = None,
    ) -> str:
        """Share a local subtree with a peer by creating a new zone.

        Flow (Alice shares with Bob):
            1. share_subtree() — create new zone, copy metadata, create DT_MOUNT
            2. RaftClient(bob).invite_zone() — Bob joins zone + creates DT_MOUNT

        Args:
            local_path: Local path to share (e.g., "/usr/alice/projectA").
            peer_addr: Peer's gRPC address (e.g., "bob:2126").
            remote_path: Where peer should mount (e.g., "/shared-projectA").
            zone_id: Explicit zone ID (auto UUID if None).

        Returns:
            The new zone's ID.
        """
        root_zone = self._mgr.root_zone_id or "root"

        # Step 1: Create zone + copy subtree + DT_MOUNT in parent
        new_zone_id: str = self._mgr.share_subtree(
            parent_zone_id=root_zone,
            path=local_path,
            zone_id=zone_id,
        )

        logger.info(
            "Shared '%s' as zone '%s', inviting peer %s",
            local_path,
            new_zone_id,
            peer_addr,
        )

        # Step 2: Invite peer to join zone + create DT_MOUNT at remote_path
        client = self._client_factory(peer_addr)
        try:
            result = await client.invite_zone(
                zone_id=new_zone_id,
                mount_path=remote_path,
                inviter_node_id=self._mgr._node_id,
                inviter_address=self._mgr._py_mgr.advertise_addr(),
            )
            logger.info(
                "Peer %s joined zone '%s' (node_id=%s)",
                peer_addr,
                new_zone_id,
                result.get("node_id"),
            )
        finally:
            await client.close()

        return new_zone_id

    async def join(
        self,
        peer_addr: str,
        remote_path: str,
        local_path: str,
    ) -> str:
        """Join a peer's shared subtree by discovering and joining its zone.

        Flow (Charlie joins via Bob):
            1. RaftClient(bob).get_metadata(remote_path) — discover DT_MOUNT
            2. RaftClient(bob).get_cluster_info(zone_id) — get leader
            3. ZoneManager.join_zone(zone_id, peers) — create local replica
            4. RaftClient(leader).join_zone() — leader adds us as Voter
            5. ZoneManager.mount(root_zone, local_path, zone_id) — DT_MOUNT

        Args:
            peer_addr: Peer's gRPC address (e.g., "bob:2126").
            remote_path: Path on peer to join (e.g., "/shared-projectA").
            local_path: Local mount point (e.g., "/usr/charlie/shared").

        Returns:
            The joined zone's ID.

        Raises:
            ValueError: If remote_path is not a DT_MOUNT on peer.
            RuntimeError: If zone discovery or join fails.
        """
        root_zone = self._mgr.root_zone_id or "root"

        # Step 1: Discover zone via peer's DT_MOUNT
        client = self._client_factory(peer_addr)
        try:
            metadata = await client.get_metadata(
                path=remote_path,
                zone_id="root",
            )

            if metadata is None:
                raise ValueError(f"Path '{remote_path}' not found on peer {peer_addr}")

            if not metadata.is_mount:
                raise ValueError(
                    f"Path '{remote_path}' on peer {peer_addr} is not a "
                    f"DT_MOUNT (type={metadata.entry_type})"
                )

            zone_id: str | None = metadata.mount_zone_id
            if not zone_id:
                raise ValueError(
                    f"DT_MOUNT at '{remote_path}' on peer {peer_addr} has no target zone_id"
                )

            logger.info(
                "Discovered zone '%s' at %s:%s",
                zone_id,
                peer_addr,
                remote_path,
            )

            # Step 2: Get cluster info for the target zone
            cluster = await client.get_cluster_info(zone_id=zone_id)
            leader_addr = cluster.get("leader_address")
            if not leader_addr:
                raise RuntimeError(f"Zone '{zone_id}' has no leader on peer {peer_addr}")
        finally:
            await client.close()

        # Step 3: Join zone locally (creates redb + Raft node, no bootstrap)
        peer_spec = f"{cluster['leader_id']}@{leader_addr}"
        self._mgr.join_zone(zone_id, peers=[peer_spec])

        logger.info("Joined zone '%s' locally, requesting Voter status", zone_id)

        # Step 4: Ask leader to add us as Voter
        leader_client = self._client_factory(leader_addr)
        try:
            await leader_client.join_zone(
                zone_id=zone_id,
                node_id=self._mgr._node_id,
                node_address=self._mgr._py_mgr.advertise_addr(),
            )
        finally:
            await leader_client.close()

        # Step 5: Mount in root zone
        self._mgr.mount(root_zone, local_path, zone_id)

        logger.info(
            "Zone '%s' mounted at '%s' — federation complete",
            zone_id,
            local_path,
        )
        return zone_id
