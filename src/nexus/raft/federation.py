"""NexusFederation — optional DI subsystem for multi-node zone sharing.

Federation is NOT kernel. It is an optional subsystem at the same level as
CacheStore/RecordStore. Without federation, NexusFS gracefully degrades to
client-server mode (REMOTE profile) or single-node standalone mode.

Layering:
    NexusFederation (this file) — orchestration / service layer
    ├── ZoneManager             — raft HAL (wraps PyO3 Rust driver)
    └── FederationClient        — native Rust gRPC peer client
                                  (nexus_kernel.FederationClient)

Two peer RPCs are exercised by this layer:
    1. NexusVFSService.Call("sys_stat") — discover zone_id from peer's DT_MOUNT
    2. ZoneApiService.JoinZone          — Raft ConfChange (add node to group)

See: docs/architecture/federation-memo.md §6.9
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.security.tls.config import ZoneTlsConfig

logger = logging.getLogger(__name__)


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
        trust_store: Any = None,
    ) -> None:
        """Initialize federation orchestrator.

        Args:
            zone_manager: ZoneManager instance for local zone operations.
            trust_store: ``nexus_kernel.TofuTrustStore`` for peer zone CA
                verification. Auto-created from the ZoneManager's
                ``tls_config`` when ``None``.
        """
        self._mgr = zone_manager
        self._trust_store = trust_store
        tls_cfg: ZoneTlsConfig | None = getattr(zone_manager, "tls_config", None)
        self._tls_config = tls_cfg

        if trust_store is None and tls_cfg is not None:
            from nexus_kernel import TofuTrustStore

            self._trust_store = TofuTrustStore(str(tls_cfg.known_zones_path))

        # Build the Rust gRPC peer client. Plaintext fallback when
        # tls_cfg is None (unit-test / single-node bootstrap path).
        from nexus_kernel import FederationClient

        if tls_cfg is not None:
            self._client = FederationClient(
                local_ca_pem=bytes(tls_cfg.ca_pem),
                node_cert_pem=bytes(tls_cfg.node_cert_pem),
                node_key_pem=bytes(tls_cfg.node_key_pem),
                tofu_store_path=str(tls_cfg.known_zones_path),
            )
        else:
            self._client = FederationClient()

        # Cluster peers (host:port) — read from NEXUS_PEERS env var (SSOT).
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
                    peers=peers,
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

        # F2 C8: return a RustMetastoreProxy so every nfs.metadata call
        # (get/put/list/delete/exists) routes via Kernel::metastore_* which
        # in turn routes via the kernel mount table to the correct
        # per-mount ZoneMetastore. The root zone handle is also attached
        # at the "/" mount below so root-zone paths (e.g. /workspace/…)
        # hit raft as well.
        metadata_store: Any
        if kernel is not None:
            from nexus.core.metastore import RustMetastoreProxy

            metadata_store = RustMetastoreProxy(kernel)
            # Root mount "/" uses the kernel's global redb metastore
            # (local, not Raft-replicated). Only explicitly mounted
            # federation zones (e.g. /corp/ → zone "corp") get per-mount
            # Raft-backed ZoneMetastores via _mount_via_kernel().

            # Set node gRPC VFS address so sys_write encodes origin in
            # backend_name (e.g. "cas-local@nexus-1:2028"). Enables
            # on-demand remote content fetch on follower nodes.
            _grpc_port = os.environ.get("NEXUS_GRPC_PORT", "2028")
            kernel.set_self_address(f"{hostname}:{_grpc_port}")
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

        Both peer RPCs go through the native Rust ``FederationClient``;
        this coroutine just offloads its blocking calls with
        ``asyncio.to_thread`` so callers can await alongside other async
        work.

        Raises:
            ValueError: If remote_path is not a DT_MOUNT on peer, or the
                local mount point is missing / already mounted.
            RuntimeError: If zone discovery or join fails.
        """
        root_zone = self._mgr.root_zone_id or ROOT_ZONE_ID
        root_store = self._mgr.get_store(root_zone)
        if root_store is None:
            raise RuntimeError(f"Root zone '{root_zone}' not found locally")

        # Fail fast on the local mount point before touching the peer —
        # we don't want to join a Raft group we can't subsequently mount.
        # local_path may live in any zone (root, or a nested child zone
        # reached via a DT_MOUNT), so we ask the Rust ZoneManager to
        # walk its live registry. Pure Rust iteration — no Python loop
        # over per-zone stores.
        _py_mgr = getattr(self._mgr, "_py_mgr", None)
        hit = _py_mgr.lookup_path(local_path) if _py_mgr is not None else None
        if hit is None:
            raise ValueError(
                f"Mount point '{local_path}' does not exist. Create the directory first (mkdir -p)."
            )
        _found_zone, _meta_bytes = hit
        # Decode to inspect is_mount — reuse the shared protobuf/JSON
        # codec used by every other Python metastore reader.
        from nexus.storage.raft_metadata_store import _deserialize_metadata

        mount_point = _deserialize_metadata(_meta_bytes)
        if mount_point.is_mount:
            raise ValueError(f"Mount point '{local_path}' is already a DT_MOUNT. Unmount first.")

        # Step 1: Discover zone via peer's DT_MOUNT (VFS sys_stat).
        metadata = await asyncio.to_thread(self._client.discover_mount, peer_addr, remote_path)
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

        # Step 2: Create local Raft node (joins existing group; no bootstrap).
        self._mgr.join_zone(zone_id, peers=[peer_addr])
        logger.info("Joined zone '%s' locally, requesting membership", zone_id)

        # Step 3: Request membership (Rust handles leader-redirect).
        node_address = getattr(self._mgr, "advertise_addr", peer_addr)
        try:
            await asyncio.to_thread(
                self._client.request_join_zone,
                peer_addr,
                zone_id,
                self._mgr.node_id,
                node_address,
            )
        except Exception:
            try:
                self._mgr.remove_zone(zone_id, force=True)
            except Exception:
                logger.warning("Failed to rollback zone '%s' after join failure", zone_id)
            raise

        # Step 4: Mount in root zone (JoinZone handler already bumped i_links_count).
        self._mgr.mount(root_zone, local_path, zone_id, increment_links=False)

        logger.info("Zone '%s' mounted at '%s' — federation complete", zone_id, local_path)
        return zone_id
