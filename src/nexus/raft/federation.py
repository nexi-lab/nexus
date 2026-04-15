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
    # Factory classmethod — called from connect() before create_nexus_fs()
    # =========================================================================

    @classmethod
    def bootstrap(
        cls,
        *,
        metadata_path: str,
        kernel: Any = None,
    ) -> tuple["NexusFederation", Any]:
        """Bootstrap federation: create ZoneManager + root zone MetastoreABC.

        Encapsulates all of connect()'s federation setup (~100 lines):
        env var parsing, TLS pre-provisioning, join token flow, ZoneManager
        creation with retry, bootstrap/join detection, static Day-1 topology.

        Args:
            metadata_path: Path to the metastore directory (used to derive zones_dir).

        Returns:
            Tuple of (NexusFederation instance, root zone MetastoreABC).

        Raises:
            ImportError: Rust extensions not available.
            RuntimeError: Raft bootstrap failed after retries.
        """
        import os
        import socket
        import time as _time
        from pathlib import Path

        from nexus.contracts.constants import DEFAULT_GRPC_BIND_ADDR
        from nexus.raft.peer_address import PeerAddress, hostname_to_node_id
        from nexus.raft.zone_manager import ZoneManager

        hostname = os.environ.get("NEXUS_HOSTNAME", socket.gethostname())
        my_id = hostname_to_node_id(hostname)
        bind_addr = os.environ.get("NEXUS_BIND_ADDR", DEFAULT_GRPC_BIND_ADDR)
        advertise_addr = os.environ.get("NEXUS_ADVERTISE_ADDR")
        zones_dir = os.environ.get("NEXUS_DATA_DIR", str(Path(metadata_path).parent / "zones"))

        # Parse peer addresses
        peers_str = os.environ.get("NEXUS_PEERS", "")
        peer_addrs = PeerAddress.parse_peer_list(peers_str) if peers_str else []
        peers = [p.to_raft_peer_str() for p in peer_addrs]

        _max_attempts = int(os.environ.get("NEXUS_STARTUP_MAX_RETRIES", "12"))
        _base_delay = 2.0
        zone_mgr: ZoneManager | None = None

        for _attempt in range(1, _max_attempts + 1):
            try:
                # TLS pre-provision (depends on leader being up)
                tls_dir_pre = Path(zones_dir) / "tls"
                token_file = tls_dir_pre / "join-token"
                if token_file.exists() and not (tls_dir_pre / "node.pem").exists():
                    join_token = token_file.read_text().strip()
                    join_peer = next(
                        (p.grpc_target for p in peer_addrs if p.node_id != my_id),
                        None,
                    )
                    if join_peer:
                        import importlib

                        _raft_mod = importlib.import_module("nexus_kernel")
                        _join_cluster = _raft_mod.join_cluster

                        logger.info(
                            "Join token found — provisioning TLS from %s (attempt %d)",
                            join_peer,
                            _attempt,
                        )
                        _join_cluster(join_peer, join_token, hostname, str(tls_dir_pre))
                        logger.info("TLS provisioning complete")
                    else:
                        raise RuntimeError("Join token found but no peer in NEXUS_PEERS to join")

                zone_mgr = ZoneManager(
                    hostname=hostname,
                    base_path=zones_dir,
                    bind_addr=bind_addr,
                    advertise_addr=advertise_addr,
                )

                # Detect joiner vs first-node
                tls_dir = Path(zones_dir) / "tls"
                is_joiner = (
                    (tls_dir / "ca.pem").exists()
                    and (tls_dir / "node.pem").exists()
                    and (tls_dir / "node-key.pem").exists()
                    and not (tls_dir / "join-token").exists()
                )

                if is_joiner:
                    zone_mgr.join_zone("root", peers=peers if peers else None)
                    logger.info("Joiner node: joined root zone (certs provisioned)")
                else:
                    zone_mgr.bootstrap(peers=peers if peers else None)

                # Static Day-1 topology from env vars (idempotent)
                zones_str = os.environ.get("NEXUS_FEDERATION_ZONES", "")
                mounts_str = os.environ.get("NEXUS_FEDERATION_MOUNTS", "")
                if zones_str:
                    zones = [z.strip() for z in zones_str.split(",") if z.strip()]
                    mounts: dict[str, str] = {}
                    if mounts_str:
                        for pair in mounts_str.split(","):
                            path, zone_id = pair.strip().split("=", 1)
                            mounts[path.strip()] = zone_id.strip()
                    zone_mgr.bootstrap_static(zones=zones, peers=peers, mounts=mounts)

                break  # Success

            except (RuntimeError, OSError, ConnectionError) as exc:
                if _attempt >= _max_attempts:
                    logger.error("Raft startup failed after %d attempts: %s", _max_attempts, exc)
                    raise
                delay = min(_base_delay * (2 ** (_attempt - 1)), 30.0)
                logger.warning(
                    "Raft startup attempt %d/%d failed: %s — retrying in %.1fs",
                    _attempt,
                    _max_attempts,
                    exc,
                    delay,
                )
                _time.sleep(delay)

        assert zone_mgr is not None  # guaranteed by loop above
        # Root zone must exist before we continue.
        _root_store = zone_mgr.get_store("root")
        if _root_store is None:
            raise RuntimeError("Root zone metastore not available after bootstrap")

        # F2 C8: return a ``RustMetastoreProxy`` so every ``nfs.metadata``
        # call (get/put/list/delete/exists) routes via
        # ``Kernel::metastore_*`` which in turn routes via the kernel
        # mount table to the correct per-mount ``ZoneMetastore``. The
        # root zone handle is also attached at the ``/`` mount below so
        # root-zone paths (e.g. ``/workspace/…``) hit raft as well.
        metadata_store: Any
        if kernel is not None:
            from nexus.core.metastore import RustMetastoreProxy

            metadata_store = RustMetastoreProxy(kernel)
            # Install the root zone's ZoneMetastore at the ``/`` mount so
            # kernel-side routing for root-zone paths reaches raft. Use
            # runtime ``getattr`` to dodge stale local type stubs while
            # an installed wheel lags the regenerated .pyi.
            try:
                import nexus_kernel as _nk

                _attach = getattr(_nk, "attach_raft_zone_to_kernel", None)
                _root_engine = getattr(_root_store, "_engine", None)
                if _attach is not None and _root_engine is not None:
                    _attach(kernel, _root_engine, "/", "root")
                    logger.info("[FED] installed root ZoneMetastore at '/' for kernel routing")
            except Exception as exc:  # pragma: no cover — logged
                logger.warning("[FED] attach_raft_zone_to_kernel('/') failed: %s", exc)
        else:
            metadata_store = _root_store

        federation = cls(zone_manager=zone_mgr)
        return federation, metadata_store

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def zone_manager(self) -> Any:
        """The underlying ZoneManager instance."""
        return self._mgr

    def ensure_topology(self) -> bool:
        """Delegate to ZoneManager.ensure_topology() for health checks."""
        result: bool = self._mgr.ensure_topology()
        return result

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
        """Cluster peer id@host:port strings for Raft group creation."""
        return [p.to_raft_peer_str() for p in self._peers]

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
