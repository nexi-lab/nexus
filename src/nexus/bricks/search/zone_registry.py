"""Zone search registry and capability advertisement (Issue #3147, Phase 1+2).

Provides:
- ZoneSearchCapabilities: Describes what search modes a zone supports.
- ZoneSearchRegistry: Maps zone_id → SearchDaemon for multi-daemon setups.

Phase 1 uses the single global daemon for all zones.
Phase 2 introduces per-zone daemons via this registry.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoneSearchCapabilities:
    """Describes the search capabilities of a zone (Issue #3147).

    Each zone may have different search hardware and indexing.
    A phone zone might only support keyword search, while a server
    zone supports hybrid + graph search with SPLADE.

    Corresponds to the planned GetSearchCapabilities RPC (Phase 2).
    """

    zone_id: str
    device_tier: str = "server"  # "phone", "laptop", "server"
    search_modes: tuple[str, ...] = ("keyword", "semantic", "hybrid")
    embedding_model: str | None = None
    embedding_dimensions: int = 0
    has_graph: bool = False
    has_splade: bool = False

    @property
    def supports_semantic(self) -> bool:
        return "semantic" in self.search_modes or "hybrid" in self.search_modes

    @property
    def supports_keyword(self) -> bool:
        return "keyword" in self.search_modes or "hybrid" in self.search_modes

    @classmethod
    def from_daemon_stats(cls, zone_id: str, daemon: Any) -> "ZoneSearchCapabilities":
        """Derive capabilities from a SearchDaemon's runtime state.

        Inspects the daemon to determine what search backends are available.
        """
        stats = daemon.get_stats() if hasattr(daemon, "get_stats") else {}
        modes = ["keyword"]  # BM25S/FTS always available

        has_db = stats.get("db_pool_size", 0) > 0
        if has_db:
            modes.append("semantic")
            modes.append("hybrid")

        has_graph = hasattr(daemon, "_graph_store")

        return cls(
            zone_id=zone_id,
            search_modes=tuple(modes),
            has_graph=has_graph,
            has_splade=False,  # Detected at startup, not from stats
            embedding_dimensions=stats.get("embedding_dimensions", 0),
        )


class ZoneSearchRegistry:
    """Maps zone_id → (SearchDaemon, capabilities) for federated search.

    Phase 1: All zones share the single global daemon.
    Phase 2: Each zone can have its own daemon with different capabilities.

    Thread-safe: mutations are rare (zone add/remove at startup),
    reads are frequent (every federated search).
    """

    def __init__(self, default_daemon: Any | None = None) -> None:
        """Initialize registry with optional default daemon.

        Args:
            default_daemon: Fallback daemon used for zones without
                a dedicated daemon (Phase 1 mode).
        """
        self._default_daemon = default_daemon
        self._daemons: dict[str, Any] = {}
        self._capabilities: dict[str, ZoneSearchCapabilities] = {}
        # Phase 2: zone_id → RPCTransport for remote zones
        self._transports: dict[str, Any] = {}

    def register(
        self,
        zone_id: str,
        daemon: Any,
        capabilities: ZoneSearchCapabilities | None = None,
    ) -> None:
        """Register a daemon for a zone.

        Args:
            zone_id: Zone identifier.
            daemon: SearchDaemon instance for this zone.
            capabilities: Explicit capabilities, or auto-detected from daemon.
        """
        self._daemons[zone_id] = daemon
        if capabilities is None:
            capabilities = ZoneSearchCapabilities.from_daemon_stats(zone_id, daemon)
        self._capabilities[zone_id] = capabilities
        logger.info(
            "[ZONE-REGISTRY] Registered zone %s: modes=%s, graph=%s",
            zone_id,
            capabilities.search_modes,
            capabilities.has_graph,
        )

    def register_remote(
        self,
        zone_id: str,
        transport: Any,
        capabilities: ZoneSearchCapabilities | None = None,
    ) -> None:
        """Register a remote zone with its gRPC transport.

        Remote zones use RPCTransport.call_rpc("search", ...) instead of
        daemon.search() directly. The SearchDelegation is sent as the
        auth_token in the gRPC call.

        Args:
            zone_id: Remote zone identifier.
            transport: RPCTransport instance connected to the remote node.
            capabilities: Zone capabilities (discovered via GetSearchCapabilities).
        """
        self._transports[zone_id] = transport
        if capabilities is not None:
            self._capabilities[zone_id] = capabilities
        logger.info("[ZONE-REGISTRY] Registered remote zone %s", zone_id)

    def get_transport(self, zone_id: str) -> Any | None:
        """Get RPCTransport for a remote zone, or None if local."""
        return self._transports.get(zone_id)

    def is_remote(self, zone_id: str) -> bool:
        """Check if a zone is served by a remote transport."""
        return zone_id in self._transports

    def unregister(self, zone_id: str) -> None:
        """Remove a zone's daemon from the registry."""
        self._daemons.pop(zone_id, None)
        self._capabilities.pop(zone_id, None)
        self._transports.pop(zone_id, None)

    def get_daemon(self, zone_id: str) -> Any | None:
        """Get the daemon for a zone, falling back to default.

        Returns None only if no daemon is registered AND no default exists.
        """
        return self._daemons.get(zone_id, self._default_daemon)

    def get_capabilities(self, zone_id: str) -> ZoneSearchCapabilities | None:
        """Get capabilities for a zone, or None if unknown."""
        return self._capabilities.get(zone_id)

    def list_zones(self) -> list[str]:
        """List all zone IDs with registered daemons."""
        return list(self._daemons.keys())

    def has_zone(self, zone_id: str) -> bool:
        """Check if a zone has a registered daemon (not counting default)."""
        return zone_id in self._daemons

    async def discover_remote_capabilities(
        self,
        zone_id: str,
        raft_client: Any,
    ) -> ZoneSearchCapabilities:
        """Discover search capabilities from a remote zone via gRPC.

        Uses RaftClient.get_search_capabilities() to call the
        GetSearchCapabilities RPC on a remote Raft node. Falls back
        to keyword-only capabilities if the RPC fails (e.g., the remote
        node is an older version without Issue #3147 support).

        Args:
            zone_id: Remote zone to query.
            raft_client: Connected RaftClient for the remote node.

        Returns:
            ZoneSearchCapabilities for the remote zone.
        """
        try:
            raw = await raft_client.get_search_capabilities(zone_id=zone_id)
            caps = ZoneSearchCapabilities(
                zone_id=raw.get("zone_id", zone_id),
                device_tier=raw.get("device_tier", "server"),
                search_modes=tuple(raw.get("search_modes", ("keyword",))),
                embedding_model=raw.get("embedding_model"),
                embedding_dimensions=raw.get("embedding_dimensions", 0),
                has_graph=raw.get("has_graph", False),
            )
        except Exception:
            logger.warning(
                "[ZONE-REGISTRY] Remote capability discovery failed for %s, "
                "defaulting to keyword-only",
                zone_id,
            )
            caps = ZoneSearchCapabilities(
                zone_id=zone_id,
                device_tier="unknown",
                search_modes=("keyword",),
            )
        self._capabilities[zone_id] = caps
        return caps

    @property
    def default_daemon(self) -> Any | None:
        return self._default_daemon

    @default_daemon.setter
    def default_daemon(self, daemon: Any) -> None:
        self._default_daemon = daemon
