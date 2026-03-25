"""NexusFederation — optional DI subsystem for multi-node zone sharing.

Federation is NOT kernel. It is an optional subsystem at the same level as
CacheStore/RecordStore. Without federation, NexusFS gracefully degrades to
client-server mode (REMOTE profile) or single-node standalone mode.

Layering:
    NexusFederation (this file) — orchestration / service layer
    ├── ZoneManager             — HAL (wraps PyO3 Rust driver)
    └── gRPC (inline)           — peer communication (discovery + join)

No separate gRPC client class needed. Federation has exactly two RPCs:
    1. NexusVFSService.Call("sys_stat") — discover zone_id from peer's DT_MOUNT
    2. ZoneApiService.JoinZone          — Raft ConfChange (add node to group)

See: docs/architecture/federation-memo.md §6.9
"""

import logging
from typing import TYPE_CHECKING, Any

import grpc
from grpc import aio as grpc_aio

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig
    from nexus.security.tls.trust_store import TofuTrustStore

logger = logging.getLogger(__name__)

# Channel options shared by all federation gRPC connections
_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 10000),
    ("grpc.keepalive_timeout_ms", 5000),
    ("grpc.keepalive_permit_without_calls", True),
    ("grpc.http2.max_pings_without_data", 0),
]


class NexusFederation:
    """Orchestrate zone sharing and joining across nodes.

    Pull model: both share and join are initiated by the local node.
    - share = create zone + DT_MOUNT locally (peer joins later)
    - join  = discover zone from peer + create local replica + request membership

    Usage:
        from nexus.raft.zone_manager import ZoneManager

        mgr = ZoneManager(hostname="nexus-1", base_path="./zones")
        mgr.bootstrap()

        fed = NexusFederation(zone_manager=mgr)

        # Share: create zone locally (peer pulls later)
        zone_id = await fed.share("/usr/alice/projectA")

        # Join: pull from peer
        zone_id = await fed.join("bob:2126", "/shared-projectA", "/usr/charlie/shared")
    """

    def __init__(
        self,
        zone_manager: Any,
        trust_store: "TofuTrustStore | None" = None,
    ) -> None:
        """Initialize federation orchestrator.

        Args:
            zone_manager: ZoneManager instance for local zone operations.
            trust_store: TOFU trust store for peer zone CA verification.
                Auto-created from ZoneManager's tls_config if None.
        """
        self._mgr = zone_manager
        self._trust_store = trust_store
        if trust_store is None:
            tls_cfg = getattr(zone_manager, "tls_config", None)
            if tls_cfg is not None:
                from nexus.security.tls.trust_store import TofuTrustStore

                self._trust_store = TofuTrustStore(tls_cfg.known_zones_path)

        # Cache TLS credentials for gRPC connections.
        self._tls_config: ZoneTlsConfig | None = getattr(zone_manager, "tls_config", None)

        # Cluster peers (host:port) — read from NEXUS_PEERS env var (SSOT).
        # Used by create_zone to include all peers in new Raft groups.
        import os

        from nexus.raft.peer_address import PeerAddress

        peers_str = os.environ.get("NEXUS_PEERS", "")
        self._peers = PeerAddress.parse_peer_list(peers_str) if peers_str else []

    # =========================================================================
    # Q3 PersistentService lifecycle (auto-managed by ServiceRegistry)
    # =========================================================================

    async def start(self) -> None:
        """Start federation service. Called by ServiceRegistry at bootstrap."""
        logger.info("Federation service started (zone_manager node_id=%s)", self._mgr.node_id)

    async def stop(self) -> None:
        """Stop federation service. Called by ServiceRegistry at shutdown."""
        logger.info("Federation service stopped")

    # =========================================================================
    # Cluster topology
    # =========================================================================

    @property
    def peers(self) -> list:
        """Cluster peer addresses (PeerAddress instances)."""
        return self._peers

    @property
    def peer_targets(self) -> list[str]:
        """Cluster peer host:port strings for Raft group creation."""
        return [p.grpc_target for p in self._peers]

    def create_zone(self, zone_id: str) -> Any:
        """Create a zone with all cluster peers included in the Raft group."""
        return self._mgr.create_zone(zone_id, peers=self.peer_targets)

    # =========================================================================
    # Public API
    # =========================================================================

    async def share(
        self,
        local_path: str,
        zone_id: str | None = None,
    ) -> str:
        """Share a local subtree by creating a new zone (pull model).

        Purely local operation. The peer joins later via join().

        Args:
            local_path: Local path to share (e.g., "/usr/alice/projectA").
            zone_id: Explicit zone ID (auto UUID if None).

        Returns:
            The new zone's ID.
        """
        root_zone = self._mgr.root_zone_id or ROOT_ZONE_ID

        new_zone_id: str = self._mgr.share_subtree(
            parent_zone_id=root_zone,
            path=local_path,
            zone_id=zone_id,
        )

        logger.info("Shared '%s' as zone '%s'", local_path, new_zone_id)
        return new_zone_id

    async def join(
        self,
        peer_addr: str,
        remote_path: str,
        local_path: str,
    ) -> str:
        """Join a peer's shared subtree (pull model).

        Flow:
            1. sys_stat(peer, remote_path) — discover DT_MOUNT → zone_id
            2. ZoneManager.join_zone(zone_id, peers=[peer_addr]) — local replica
            3. JoinZone RPC to peer — Raft ConfChange (auto-forwarded to leader)
            4. ZoneManager.mount(root, local_path, zone_id) — local DT_MOUNT

        Only 1 discovery RPC + 1 join RPC. No separate leader discovery needed
        (JoinZone auto-forwards to leader via RaftNotLeaderError).

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
        root_zone = self._mgr.root_zone_id or ROOT_ZONE_ID

        # Step 0: Validate local mount point before any remote operations.
        # Fail fast so we don't join a Raft group we can't mount.
        root_store = self._mgr.get_store(root_zone)
        if root_store is None:
            raise RuntimeError(f"Root zone '{root_zone}' not found locally")
        mount_point = root_store.get(local_path)
        if mount_point is None:
            raise ValueError(
                f"Mount point '{local_path}' does not exist in root zone. "
                f"Create the directory first (mkdir -p)."
            )
        if mount_point.is_mount:
            raise ValueError(f"Mount point '{local_path}' is already a DT_MOUNT. Unmount first.")

        # Step 1: Discover zone via peer's DT_MOUNT (VFS layer: sys_stat)
        metadata = await self._discover_mount(peer_addr, remote_path)

        if metadata is None:
            raise ValueError(f"Path '{remote_path}' not found on peer {peer_addr}")

        if not metadata.get("is_mount") and metadata.get("entry_type") != 2:
            raise ValueError(
                f"Path '{remote_path}' on peer {peer_addr} is not a "
                f"DT_MOUNT (type={metadata.get('entry_type')})"
            )

        zone_id: str | None = metadata.get("target_zone_id")
        if not zone_id:
            raise ValueError(
                f"DT_MOUNT at '{remote_path}' on peer {peer_addr} has no target zone_id"
            )

        logger.info("Discovered zone '%s' at %s:%s", zone_id, peer_addr, remote_path)

        # Step 2: Create local Raft node (no bootstrap, joins existing group)
        self._mgr.join_zone(zone_id, peers=[peer_addr])
        logger.info("Joined zone '%s' locally, requesting membership", zone_id)

        # Step 3: Request membership via JoinZone RPC (ConfChange)
        # Auto-forwarded to leader if peer is a follower.
        try:
            await self._request_membership(
                peer_addr=peer_addr,
                zone_id=zone_id,
                node_id=self._mgr.node_id,
                node_address=getattr(self._mgr, "advertise_addr", peer_addr),
            )
        except Exception:
            # Rollback: remove local zone if membership request failed
            try:
                self._mgr.remove_zone(zone_id, force=True)
            except Exception:
                logger.warning("Failed to rollback zone '%s' after join failure", zone_id)
            raise

        # Step 4: Mount in root zone (JoinZone handler already incremented i_links_count)
        self._mgr.mount(root_zone, local_path, zone_id, increment_links=False)

        logger.info("Zone '%s' mounted at '%s' — federation complete", zone_id, local_path)
        return zone_id

    # =========================================================================
    # Private: gRPC helpers (no external client class needed)
    # =========================================================================

    def _build_channel(self, address: str) -> grpc_aio.Channel:
        """Create an async gRPC channel with optional mTLS.

        Uses TOFU trust store CA bundle when available, so federation
        works across zones with different CAs.
        """
        if self._tls_config is not None:
            ca_pem = self._tls_config.ca_pem
            # Use TOFU CA bundle if trust store has trusted zones
            if self._trust_store is not None:
                bundle = self._trust_store.build_ca_bundle(self._tls_config.ca_cert_path)
                ca_pem = bundle.read_bytes()
            creds = grpc.ssl_channel_credentials(
                root_certificates=ca_pem,
                private_key=self._tls_config.node_key_pem,
                certificate_chain=self._tls_config.node_cert_pem,
            )
            return grpc_aio.secure_channel(address, creds, options=_CHANNEL_OPTIONS)
        return grpc_aio.insecure_channel(address, options=_CHANNEL_OPTIONS)

    async def _discover_mount(self, peer_addr: str, path: str) -> dict | None:
        """Discover a DT_MOUNT's target zone_id via VFS sys_stat on peer.

        Uses NexusVFSService.Call("sys_stat") — the standard VFS metadata path.
        """
        from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
        from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

        channel = self._build_channel(peer_addr)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            payload = encode_rpc_message({"path": path})
            request = vfs_pb2.CallRequest(method="sys_stat", payload=payload)

            response = await stub.Call(request, timeout=10.0)

            if response.is_error:
                result = decode_rpc_message(response.payload)
                logger.warning("sys_stat(%s) on %s failed: %s", path, peer_addr, result)
                return None

            return decode_rpc_message(response.payload)
        except grpc.RpcError as e:
            logger.error("Discovery RPC to %s failed: %s", peer_addr, e)
            raise RuntimeError(f"Cannot reach peer {peer_addr}: {e}") from e
        finally:
            await channel.close()

    async def _request_membership(
        self,
        peer_addr: str,
        zone_id: str,
        node_id: int,
        node_address: str,
    ) -> None:
        """Send JoinZone ConfChange RPC to peer (auto-forwarded to leader).

        Uses ZoneApiService.JoinZone — Raft protocol layer.
        """
        from nexus.raft import transport_pb2, transport_pb2_grpc

        channel = self._build_channel(peer_addr)
        try:
            stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
            request = transport_pb2.JoinZoneRequest(
                zone_id=zone_id,
                node_id=node_id,
                node_address=node_address,
            )

            response = await stub.JoinZone(request, timeout=10.0)

            if not response.success and response.leader_address:
                # Follower redirected us to leader — retry with leader
                logger.info(
                    "Redirected to leader %s for zone '%s'",
                    response.leader_address,
                    zone_id,
                )
                await channel.close()
                await self._request_membership(
                    peer_addr=response.leader_address,
                    zone_id=zone_id,
                    node_id=node_id,
                    node_address=node_address,
                )
                return

            if not response.success:
                raise RuntimeError(f"JoinZone failed: {response.error}")

            logger.info("Membership granted for zone '%s'", zone_id)
        except grpc.RpcError as e:
            logger.error("JoinZone RPC to %s failed: %s", peer_addr, e)
            raise RuntimeError(f"Cannot join zone '{zone_id}' via {peer_addr}: {e}") from e
        finally:
            await channel.close()
